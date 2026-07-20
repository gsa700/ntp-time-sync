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

1. Download the `.exe` and put it in a **stable, writable folder** — the recommended spot is
   `%LOCALAPPDATA%\Programs\NTP Time Sync\` (create it if needed). A fixed location keeps
   Start-at-logon and the tray-visibility setting working, and lets the app **update itself in place**.
2. Double-click it — a colored dot appears in the system tray.
3. **Windows SmartScreen** may say *"Windows protected your PC"* because the app isn't
   code-signed. Click **More info → Run anyway**. It's open source; every line is in this repo.
4. If no dot appears, see [Make the icon visible](#make-the-icon-visible-windows-11) below.
5. **Start at logon** is on by default; change it (and update checks) from the right-click menu.

First run creates its settings at `%APPDATA%\NTP Time Sync\config.json`.

## Make the icon visible (Windows 11)

Usually nothing to do — the dot normally appears on its own. But Windows 11 can
**hide new tray icons**, in which case the app is running and the dot simply isn't
on the taskbar. If that happens:

**Settings → Personalization → Taskbar → Other system tray icons →** turn the
**NTP Time Sync** entry **On**.

One-time and per-app; it sticks afterward. (Windows 10 shows the icon automatically.)

## The light

| Color  | Meaning |
|--------|---------|
| 🟢 Green  | Clock accurate — \|offset\| < 1 s vs. the reference server |
| 🟡 Yellow | Drifting (1–2 s), the Windows Time service isn't running, not NTP-synced (on the free-running CMOS clock), or last sync stale (> 40 min) |
| 🔴 Red    | \|offset\| > 2 s, or the reference server is unreachable |
| ⚪ Gray   | Starting up / probe error |

The light follows your **clock's actual accuracy** (the measured offset), so it works
no matter which NTP server Windows itself uses — you don't have to match the server
below. It separately flags the case that started this project: Windows silently
falling back to the free-running CMOS clock.

Hover the icon for a one-line summary; **left-click** to open the panel.

## The panel

**Left-click** the tray dot to open the status panel — a solid status-colored
header over the live readout, with every action one click away:

<img src="docs/panel.png" alt="NTP Time Sync status panel" width="300">

- **Colored header** — green / yellow / red with the reason, readable at a glance
- **Readout** — offset, server, source, last sync (updates live while open)
- **Refresh** — re-probe immediately
- **Resync (admin)** — starts the time service if needed, then `w32tm /resync /force`;
  opens an elevated PowerShell (UAC)
- **Start service (admin)** — appears only when the Windows Time service is stopped;
  offers to also set it to start at boot (see below)
- **Configure… (admin)** — change the NTP server, then applies it elevated (UAC)
- **Open time.is** — browser sanity check
- **Close**

**Right-click** the dot for the rest: **Start at logon** (on by default),
**Check for updates**, **Auto-check on startup** (off by default), and **Quit**.

When a newer release exists, **Check for updates** offers to **download and install
it in place**, then asks to **Restart now** to finish (or choose No — it also starts
at your next sign-in). No manual re-download; no admin needed. Auto-check only
*notifies* — installing is always a click.

Polling is read-only and runs **non-elevated**; only Resync and Configure raise
a UAC prompt on demand.

## Run from source (developers)

Requires **Python 3.8+** on Windows.

```
pip install -r requirements.txt
pythonw ntp_time_sync.pyw
```

Double-clicking `ntp_time_sync.pyw` also works (runs windowless via `pythonw`).

Running from source never touches the installed app's startup entry: it uses its
own `NtpTimeSync-dev` registry value, and unlike the `.exe` it does **not** enable
Start-at-logon on first run — toggle it from the menu if you want it. Its
`config.json` also lives next to the script, not in `%APPDATA%`, so a dev instance
and an installed copy keep separate settings.

### Build & update your install

One command rebuilds the exe and updates your installed copy (stops it,
overwrites `%LOCALAPPDATA%\Programs\NTP Time Sync\`, relaunches). Settings in
`%APPDATA%` are preserved:

```
.\build.ps1              # build + update your install
.\build.ps1 -NoDeploy    # just build into dist\
```

First-time build deps: `pip install pyinstaller`.

### Cut a release (for sharing)

After `.\build.ps1 -NoDeploy`, publish the exe so others can download it:

```
Copy-Item "dist\NTP Time Sync.exe" "dist\NTP-Time-Sync.exe" -Force
gh release create vX.Y.Z "dist\NTP-Time-Sync.exe" --title "NTP Time Sync vX.Y.Z" --notes "..."
```

`README` links to `/releases/latest`, so it always points at the newest.

## When the time service isn't running

A fresh, non-domain Windows install leaves **Windows Time set to start manually**
(trigger-start), and it is often simply *stopped*. Nothing looks wrong — but nothing
is disciplining your clock either, so it free-runs and drifts. This is a common
cause of "signals but no decodes."

The app shows this as **yellow — "Windows Time service not running"**, and the panel
grows a **Start service (admin)** button. Because a manually-started service can stop
again after a reboot, that button also offers to set it to **start automatically**.
That's a persistent change to a system service, so it's always an explicit choice —
answer **No** to just start it this once.

Every `w32tm /query` fails with `0x80070426` ("The service has not been started")
while it's stopped, which is why **Resync** starts the service before resyncing.

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
  "stale_minutes": 40,
  "dns_cache_minutes": 30,
  "auto_check_updates": false,
  "require_server": false
}
```

- **server** — the **reference** the app measures your clock offset against. Any NTP
  host or IP; public pool by default. Point it at a LAN time server if you run one
  (e.g. `192.0.2.10` or a GPS-disciplined NTP box).
- **green_max_offset / yellow_max_offset** — thresholds in seconds.
- **poll_seconds** — how often to probe.
- **stale_minutes** — if the last successful sync is older than this, don't show green.
- **dns_cache_minutes** — how long to reuse the server's resolved IP. A hostname would
  otherwise be looked up on *every* poll — at the default 45 s that's ~1,900 DNS queries
  a day, enough to look like abuse to a local resolver (`pool.ntp.org` in particular
  rotates answers on a short TTL). Pinning one address also keeps consecutive samples
  on the same server. A failed probe re-resolves immediately, so a dead address
  self-corrects. Set `0` to resolve every poll; ignored when the server is an IP.
- **auto_check_updates** — check GitHub for a newer release at startup (toggle from the menu).
- **require_server** — if `true`, also warn (yellow) unless Windows is syncing to this
  exact server. Off by default (the light follows your clock's accuracy regardless of
  which server Windows uses). Turn it on if you run a dedicated source and want to be
  told when Windows isn't using it.

The file also stores an internal `logon_initialized` flag (managed automatically — leave it).

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
