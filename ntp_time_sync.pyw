#!/usr/bin/env pythonw
"""NTP Time Sync - a Windows system-tray light for Windows Time (w32time) health.

Tri-color light:
  GREEN  - synced to the configured NTP server and |offset| < green_max
  YELLOW - synced but drifting (green_max..yellow_max) or last sync is stale
  RED    - wrong source / not synced / |offset| > yellow_max / server unreachable

Motivating use case: FT8/FT4 digital modes (WSJT-X) need the PC clock within
about 1 second of UTC or nothing decodes, even with strong signals. This puts
that health check in the system tray at a glance.

Reading status needs no admin, so polling runs quietly non-elevated.
"Resync" and "Configure server" shell out to an elevated PowerShell (UAC
prompt) only when you click them.

Config lives in config.json next to this script (see config.example.json).
"""

# Copyright (C) 2026 David Erickson (AB0R)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import re
import sys
import json
import time
import shutil
import socket
import tempfile
import threading
import subprocess
import webbrowser
import datetime as dt
import ctypes

from PIL import Image, ImageDraw
import pystray

APP_NAME = "NTP Time Sync"
APP_VERSION = "1.3.16"
REPO = "gsa700/ntp-time-sync"
RELEASES_URL = "https://github.com/%s/releases/latest" % REPO
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
FROZEN = getattr(sys, "frozen", False)

# --------------------------------------------------------------- install model
# One exe, three run modes decided at launch:
#   portable  - a `portable.txt` marker sits beside the exe (the .zip edition).
#               Config lives next to the exe; nothing is installed, no Run key,
#               no Add/Remove entry. Runs from a USB stick and leaves no trace.
#   installed - running from the per-user install dir under %LOCALAPPDATA%.
#               Config in %APPDATA%, starts at logon, listed in Add/Remove.
#   loose     - a bare exe run from Downloads etc. On first run it offers to
#               install itself; if declined it runs once in place.
INSTALL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", APP_NAME)
INSTALL_EXE = os.path.join(INSTALL_DIR, "NTP-Time-Sync.exe")
PORTABLE_MARKER = "portable.txt"


def _same_path(a, b):
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def _exe_dir():
    return os.path.dirname(os.path.abspath(sys.executable))


IS_PORTABLE = FROZEN and os.path.exists(os.path.join(_exe_dir(), PORTABLE_MARKER))
IS_INSTALLED = FROZEN and _same_path(sys.executable, INSTALL_EXE)
IS_LOOSE = FROZEN and not IS_PORTABLE and not IS_INSTALLED

# Config location follows the run mode. A packaged app may live somewhere
# read-only, and __file__ points into a temp extraction dir, so only the
# portable edition (which owns its folder) keeps config beside the exe.
if not FROZEN:
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))   # dev: next to script
elif IS_PORTABLE:
    CONFIG_DIR = _exe_dir()                                    # portable: next to exe
else:
    CONFIG_DIR = os.path.join(                                 # installed / loose
        os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
try:
    os.makedirs(CONFIG_DIR, exist_ok=True)
except OSError:
    CONFIG_DIR = os.path.expanduser("~")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CREATE_NO_WINDOW = 0x08000000

DEFAULTS = {
    "server": "pool.ntp.org",   # any NTP host or IP; e.g. a LAN server
    "poll_seconds": 45,
    "green_max_offset": 1.0,    # abs seconds - FT8 needs < ~1s
    "yellow_max_offset": 2.0,   # abs seconds
    "stale_minutes": 40,        # last sync older than this -> not green
    "dns_cache_minutes": 30,    # reuse the resolved IP this long (0 = resolve every poll)
    "auto_check_updates": False,  # check GitHub for a newer release at startup
    "start_at_logon": True,       # desired startup state; the installed copy keeps
                                  # the Run key reconciled to match this each launch
    "require_server": False,      # if True, warn unless Windows syncs to this exact server
}

# --------------------------------------------------------------- config io

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        save_config(cfg)
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------- w32tm probes

def _run(args, timeout=15):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _parse_time(val):
    if not val or "unspecified" in val.lower():
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


_dns_cache = {}                       # host -> (ip, expiry from time.monotonic())
_dns_lock = threading.Lock()


def _is_ip_literal(host):
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except (OSError, ValueError):
            continue
    return False


def resolve_server(host, ttl_minutes=30):
    """Resolve host to an IP, caching the answer so polling doesn't hammer DNS.

    w32tm resolves the name itself on every call, and pool.ntp.org hands out
    rotating answers with a short TTL - at a 45 s poll that is ~1900 lookups a
    day from one machine, which looks like abuse to a local resolver. Pinning
    one address between lookups also means consecutive samples come from the
    same server, which is what you want when measuring offset.

    Returns the host unchanged if it is already an IP, if caching is disabled,
    or if resolution fails (let w32tm try and report the error as before).
    """
    if not host or _is_ip_literal(host):
        return host
    if ttl_minutes <= 0:
        return host
    now = time.monotonic()
    with _dns_lock:
        hit = _dns_cache.get(host)
        if hit is not None and now < hit[1]:
            return hit[0]
    try:
        infos = socket.getaddrinfo(host, 123, 0, socket.SOCK_DGRAM)
    except (socket.gaierror, OSError):
        return host
    if not infos:
        return host
    ip = infos[0][4][0]
    with _dns_lock:
        _dns_cache[host] = (ip, now + ttl_minutes * 60)
    return ip


def forget_resolved(host):
    """Drop a cached address so the next probe re-resolves.

    Called when a probe fails: the pinned address may have gone away, and a
    fresh lookup can hand back a different, working one.
    """
    with _dns_lock:
        _dns_cache.pop(host, None)


def service_running():
    """True/False if w32time is running, None if it can't be determined.

    Uses the numeric STATE code from `sc query`, which is the same on localized
    Windows even though the word next to it is translated. Read-only, no admin.
    A stopped w32time makes every `w32tm /query` fail with 0x80070426, so this
    has to be asked separately rather than inferred from an empty status.
    """
    try:
        p = _run(["sc", "query", "w32time"], timeout=8)
        m = re.search(r"STATE\s+:\s+(\d+)", p.stdout or "")
        if not m:
            return None
        return m.group(1) == "4"          # 4 = SERVICE_RUNNING, 1 = STOPPED
    except Exception:
        return None


def service_autostart():
    """True if w32time is set to start automatically, False if not, None if unknown.

    Reads the Start value straight from the registry (2 = Automatic,
    3 = Manual/trigger, 4 = Disabled) so it doesn't depend on `sc` wording.
    A fresh non-domain Windows install ships this as 3, which is why the clock
    can quietly stop syncing with nothing visibly wrong.
    """
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\W32Time") as k:
            return winreg.QueryValueEx(k, "Start")[0] == 2
    except OSError:
        return None


def query_status():
    out = {"source": None, "last_sync": None, "stratum": None}
    try:
        p = _run(["w32tm", "/query", "/status"])
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("Source:"):
                out["source"] = line.split(":", 1)[1].strip()
            elif line.startswith("Stratum:"):
                out["stratum"] = line.split(":", 1)[1].strip()
            elif line.startswith("Last Successful Sync Time:"):
                out["last_sync"] = _parse_time(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return out


def probe_offset(server, dns_ttl_minutes=30):
    """Return (offset_seconds or None, reachable bool).

    Probes the cached IP rather than the hostname so each poll doesn't trigger
    a fresh DNS lookup; a failure drops the cache entry so the next poll
    re-resolves.
    """
    if not server:
        return (None, False)
    target = resolve_server(server, dns_ttl_minutes)
    try:
        p = _run(["w32tm", "/stripchart", "/computer:%s" % target,
                  "/samples:1", "/dataonly"], timeout=12)
        text = (p.stdout or "") + (p.stderr or "")
        if "error" in text.lower():
            forget_resolved(server)
            return (None, False)
        matches = re.findall(r"([+-]?\d+\.\d+)s\b", text)
        if not matches:
            forget_resolved(server)
            return (None, False)
        return (float(matches[-1]), True)
    except Exception:
        forget_resolved(server)
        return (None, False)


# --------------------------------------------------------------- evaluation

def evaluate(cfg):
    """Return a dict of display fields + the traffic-light color.

    The light is driven by the measured clock offset (real accuracy) against the
    configured reference server, so it works no matter which NTP server Windows
    itself uses. A source that has fallen back to the free-running CMOS clock is
    flagged even when the offset still looks OK. Requiring Windows to sync to
    this exact server is opt-in via cfg["require_server"].
    """
    st = query_status()
    server = cfg["server"]
    offset, reachable = probe_offset(server, cfg.get("dns_cache_minutes", 30))
    svc_up = service_running()
    source = st["source"] or ""
    low = source.lower()
    not_ntp = (not source) or ("cmos" in low) or ("free-running" in low)

    age_min = None
    if st["last_sync"] is not None:
        age_min = (dt.datetime.now() - st["last_sync"]).total_seconds() / 60.0
        stale = age_min > cfg["stale_minutes"]
    else:
        stale = True

    if not reachable or offset is None:
        color, reason = "red", "%s unreachable" % server
    else:
        a = abs(offset)
        wrong = cfg.get("require_server", False) and bool(server) and server not in source
        if a >= cfg["yellow_max_offset"]:
            color, reason = "red", "offset %+.3f s" % offset
        elif svc_up is False:
            # Checked before the CMOS case: with the service stopped the source
            # is empty for a different reason, and the fix is a different button.
            color, reason = "yellow", "Windows Time service not running"
        elif not_ntp:
            color, reason = "yellow", "not NTP-synced (CMOS clock)"
        elif wrong:
            color, reason = "yellow", "synced to %s, not %s" % (source, server)
        elif a >= cfg["green_max_offset"]:
            color, reason = "yellow", "drifting"
        elif stale:
            color, reason = "yellow", "sync stale"
        else:
            color, reason = "green", "healthy"

    offset_txt = ("%+.3f s" % offset) if offset is not None else "unreachable"
    if st["last_sync"]:
        lastsync_txt = st["last_sync"].strftime("%m/%d %H:%M:%S")
        if age_min is not None:
            lastsync_txt += "  (%.0f min ago)" % age_min
    else:
        lastsync_txt = "n/a"

    if source:
        source_txt = source
    elif svc_up is False:
        source_txt = "service not running"
    else:
        source_txt = "n/a"

    return {
        "color": color, "reason": reason, "server": server,
        "offset_txt": offset_txt, "source": source_txt,
        "lastsync_txt": lastsync_txt, "svc_up": svc_up,
    }


# --------------------------------------------------------------- icon art

_ICON_COLORS = {
    "green":  (46, 204, 113),
    "yellow": (241, 196, 15),
    "red":    (231, 76, 60),
    "gray":   (127, 127, 127),
}


def make_icon(color):
    rgb = _ICON_COLORS.get(color, _ICON_COLORS["gray"])
    size, pad = 64, 6
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([pad - 2, pad - 2, size - pad + 2, size - pad + 2],
              fill=(40, 40, 40, 180))                      # dark ring for contrast
    d.ellipse([pad, pad, size - pad, size - pad], fill=rgb + (255,))
    d.ellipse([pad + 8, pad + 6, pad + 24, pad + 22],
              fill=(255, 255, 255, 90))                    # highlight
    return img


# --------------------------------------------------------------- elevation

def run_elevated_ps(ps_command):
    """Launch an elevated PowerShell (UAC) that runs ps_command and stays open."""
    params = '-NoExit -NoProfile -Command "%s"' % ps_command
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", params, None, 1)


def action_resync():
    # Start the service first: on a stopped w32time a bare /resync just fails
    # with 0x80070426 and leaves the user stuck. Starting an already-running
    # service is a no-op, so this is safe either way.
    run_elevated_ps("Start-Service w32time; "
                    "w32tm /resync /force; "
                    "w32tm /query /status")


def action_start_service(set_automatic=False):
    """Start w32time, optionally making it start at boot from then on."""
    ps = "Start-Service w32time; "
    if set_automatic:
        ps += "Set-Service w32time -StartupType Automatic; "
    ps += "w32tm /resync /force; w32tm /query /status"
    run_elevated_ps(ps)


def action_configure_apply(server):
    ps = ("w32tm /config /manualpeerlist:'%s,0x8' /syncfromflags:manual /update; "
          "Restart-Service w32time; "
          "w32tm /resync /force; "
          "w32tm /query /status" % server)
    run_elevated_ps(ps)


# --------------------------------------------------------------- start at logon
#
# Autostart uses a Startup-folder shortcut, not a Run-key value. On a real
# machine the Run-key entry silently never fired at logon (the process was never
# created), while a Startup-folder shortcut launched reliably -- and unlike a
# scheduled task it needs no elevation. A dev checkout still uses a Run-key value
# so it can toggle startup without dropping a .lnk in the Startup folder.

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "NtpTimeSync" if FROZEN else "NtpTimeSync-dev"


def _logon_command():
    """Dev-mode Run-key command: pythonw + script."""
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def _set_run_key(enable):
    """Create the dev Run-key value, or remove it. Also used to clear a legacy
    value left by older installed builds so it can't double-launch the shortcut."""
    import winreg
    if enable:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _logon_command())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, RUN_VALUE)
        except OSError:
            pass


def _startup_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _startup_shortcut():
    return os.path.join(_startup_dir(), APP_NAME + ".lnk")


def _write_shortcut(target):
    """Create/refresh the Startup-folder .lnk via the Shell COM (no extra Python
    deps). Called only when the shortcut is missing, so it isn't a per-launch
    cost; the hidden PowerShell keeps it off-screen."""
    lnk = _startup_shortcut()
    q = lambda s: s.replace("'", "''")
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        "$s=$w.CreateShortcut('%s');"
        "$s.TargetPath='%s';$s.WorkingDirectory='%s';"
        "$s.Description='%s';$s.Save()"
        % (q(lnk), q(target), q(os.path.dirname(target)), APP_NAME))
    try:
        os.makedirs(_startup_dir(), exist_ok=True)
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       creationflags=CREATE_NO_WINDOW, timeout=25)
    except Exception:
        pass


def _remove_shortcut():
    try:
        os.remove(_startup_shortcut())
    except OSError:
        pass


def logon_enabled():
    if not FROZEN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, RUN_VALUE)
                return True
        except OSError:
            return False
    return os.path.exists(_startup_shortcut())


def set_logon(enable):
    """Menu / install entry point: record intent and make autostart match. Frozen
    app uses a Startup-folder shortcut; a dev checkout uses a Run-key value."""
    try:
        state.cfg["start_at_logon"] = bool(enable)
        save_config(state.cfg)
    except Exception:
        pass
    if not FROZEN:
        _set_run_key(enable)
        return
    _set_run_key(False)                      # clear any legacy Run-key value
    if enable:
        _write_shortcut(INSTALL_EXE if IS_INSTALLED else sys.executable)
    else:
        _remove_shortcut()


def reconcile_logon():
    """Installed copy: drop any legacy Run-key value and make the Startup shortcut
    match intent. Creates it when wanted-but-missing (also the migration path from
    an older Run-key install); removes it when startup is turned off."""
    _set_run_key(False)
    if bool(state.cfg.get("start_at_logon", True)):
        if not os.path.exists(_startup_shortcut()):
            _write_shortcut(INSTALL_EXE)
    else:
        _remove_shortcut()


# --------------------------------------------------------------- install / uninstall

ARP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s" % APP_NAME
_MB_YESNO = 0x4
_MB_ICONQUESTION = 0x20
_MB_ICONINFO = 0x40
_MB_ICONWARN = 0x30
_MB_TOPMOST = 0x40000
_MB_SETFG = 0x10000
_IDYES = 6


def _msgbox(text, flags=_MB_ICONINFO):
    return ctypes.windll.user32.MessageBoxW(
        0, text, APP_NAME, flags | _MB_TOPMOST | _MB_SETFG)


def register_uninstall():
    """Write the Add/Remove Programs entry for the installed copy."""
    import winreg
    try:
        size_kb = int(os.path.getsize(INSTALL_EXE) / 1024)
    except OSError:
        size_kb = 0
    vals = {
        "DisplayName": APP_NAME,
        "DisplayVersion": APP_VERSION,
        "Publisher": "David Erickson (AB0R)",
        "DisplayIcon": INSTALL_EXE,
        "InstallLocation": INSTALL_DIR,
        "UninstallString": '"%s" --uninstall' % INSTALL_EXE,
        "QuietUninstallString": '"%s" --uninstall --quiet' % INSTALL_EXE,
        "URLInfoAbout": "https://github.com/%s" % REPO,
    }
    dwords = {"NoModify": 1, "NoRepair": 1, "EstimatedSize": size_kb}
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ARP_KEY) as k:
        for name, v in vals.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, v)
        for name, v in dwords.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, v)


def unregister_uninstall():
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, ARP_KEY)
    except OSError:
        pass


def refresh_uninstall_entry():
    """Keep the Add/Remove version in step with the running exe -- self-update
    overwrites the exe in place, so the stored DisplayVersion would otherwise go
    stale."""
    try:
        register_uninstall()
    except Exception:
        pass


def do_install():
    """Copy this exe into the per-user install dir, wire up startup + the
    Add/Remove entry, launch the installed copy, and return True. On failure
    returns False and the caller keeps running in place."""
    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        shutil.copy2(sys.executable, INSTALL_EXE)
    except Exception:
        return False
    try:
        state.cfg["start_at_logon"] = True
        save_config(state.cfg)
        _set_run_key(False)                # ensure no legacy Run-key value lingers
        _write_shortcut(INSTALL_EXE)       # autostart points at the installed copy
        register_uninstall()
    except Exception:
        pass
    try:
        os.startfile(INSTALL_EXE)          # launch installed copy; this one exits
    except Exception:
        return False
    return True


def offer_install():
    """First-run prompt for a loose exe. Returns True if it installed and
    relaunched (caller should exit), False to keep running in place."""
    if os.path.exists(INSTALL_EXE):
        # Already installed -- don't make a second copy; just hand off to it.
        try:
            os.startfile(INSTALL_EXE)
            return True
        except Exception:
            return False
    choice = _msgbox(
        "Install %s?\n\n"
        "It will be copied to your user profile, start automatically at logon, "
        "and appear in 'Installed apps' so you can remove it later.\n\n"
        "Yes  -  Install\n"
        "No   -  Just run once from here" % APP_NAME,
        _MB_YESNO | _MB_ICONQUESTION)
    if choice == _IDYES:
        if do_install():
            return True
        _msgbox("Install didn't complete; running in place instead.", _MB_ICONWARN)
    return False


def _schedule_self_delete():
    """A running exe can't delete itself; hand off to a detached batch that waits
    for us to exit, kills any lingering tray instance, removes the install dir,
    then deletes itself."""
    cmd_path = os.path.join(tempfile.gettempdir(), "ntp_uninstall.cmd")
    script = (
        "@echo off\r\n"
        "ping 127.0.0.1 -n 3 >nul\r\n"                       # ~2s: let us exit
        'taskkill /f /im "NTP-Time-Sync.exe" >nul 2>&1\r\n'  # stop the tray
        "ping 127.0.0.1 -n 2 >nul\r\n"
        'rmdir /s /q "%s" >nul 2>&1\r\n'
        'del /q "%%~f0" >nul 2>&1\r\n'
    ) % INSTALL_DIR
    try:
        with open(cmd_path, "w", encoding="ascii") as f:
            f.write(script)
        subprocess.Popen(
            ["cmd", "/c", cmd_path],
            creationflags=CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
    except Exception:
        pass


def do_uninstall():
    """Reverse an install: remove the Run key + Add/Remove entry, optionally the
    settings, and self-delete the install dir. Invoked via `--uninstall` (the
    Add/Remove button's UninstallString)."""
    quiet = "--quiet" in sys.argv
    if not quiet:
        if _msgbox("Remove %s from this computer?" % APP_NAME,
                   _MB_YESNO | _MB_ICONQUESTION) != _IDYES:
            return
    _set_run_key(False)
    _remove_shortcut()
    unregister_uninstall()
    remove_settings = quiet
    if not quiet:
        remove_settings = _msgbox(
            "Also remove your saved settings (server, thresholds)?",
            _MB_YESNO | _MB_ICONQUESTION) == _IDYES
    if remove_settings:
        appdata_cfg = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
        try:
            shutil.rmtree(appdata_cfg)
        except OSError:
            pass
    _schedule_self_delete()
    if not quiet:
        _msgbox("%s has been removed." % APP_NAME)


# --------------------------------------------------------------- app state

class State:
    def __init__(self):
        self.cfg = load_config()
        self.color = "gray"
        self.reason = "starting..."
        self.server = self.cfg["server"]
        self.offset_txt = "..."
        self.source = "..."
        self.lastsync_txt = "..."
        self.svc_up = None
        self.icon = None
        self.stop = threading.Event()

    def apply(self, fields):
        self.color = fields["color"]
        self.reason = fields["reason"]
        self.server = fields["server"]
        self.offset_txt = fields["offset_txt"]
        self.source = fields["source"]
        self.lastsync_txt = fields["lastsync_txt"]
        self.svc_up = fields.get("svc_up")

    def tooltip(self):
        return "%s v%s\n%s  %s\n%s" % (
            APP_NAME, APP_VERSION, self.color.upper(), self.offset_txt, self.server)


state = State()


def refresh_once():
    try:
        state.apply(evaluate(state.cfg))
    except Exception as e:
        state.color, state.reason = "gray", "error: %s" % e
    if state.icon is not None:
        state.icon.icon = make_icon(state.color)
        state.icon.title = state.tooltip()
        state.icon.update_menu()
    try:
        panel.refresh()
    except Exception:
        pass


def poll_loop():
    while not state.stop.is_set():
        refresh_once()
        state.stop.wait(state.cfg["poll_seconds"])


# --------------------------------------------------------------- menu handlers

def on_quit(icon, item):
    state.stop.set()
    if panel is not None:
        panel.close()
    icon.stop()


# --------------------------------------------------------------- status panel

class StatusPanel:
    """Borderless popup with a solid status-colored header + live readout.

    Runs its own Tk root on a dedicated thread; every Tk call is marshaled onto
    that thread with root.after(), so pystray's thread never touches Tk directly.
    """

    BODY_BG = "#f7f7f7"
    LABEL_FG = "#555555"
    VALUE_FG = "#1a1a1a"
    BORDER = "#c8c8c8"

    def __init__(self):
        self.root = None
        self.win = None
        self.hdr = None
        self.hdr_lbl = None
        self.btn_startsvc = None
        self.rows = {}
        self._ready = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(5)

    def _run(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception:
            self.root = None
        finally:
            self._ready.set()
        if self.root is not None:
            self.root.mainloop()

    # ---- thread-safe entry points ----
    def show(self):
        if self.root is not None:
            self.root.after(0, self._show)

    def refresh(self):
        if self.root is not None and self.win is not None:
            self.root.after(0, self._update)

    def close(self):
        if self.root is not None:
            try:
                self.root.after(0, self.root.quit)
            except Exception:
                pass

    # ---- Tk-thread internals ----
    def _hexcolor(self, color):
        return "#%02x%02x%02x" % _ICON_COLORS.get(color, _ICON_COLORS["gray"])

    def _build(self):
        import tkinter as tk
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.configure(bg=self.BORDER)
        outer = tk.Frame(self.win, bg=self.BORDER)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        self.hdr = tk.Frame(outer, bg="#888888")
        self.hdr.pack(fill="x")
        self.hdr_lbl = tk.Label(self.hdr, text="", font=("Segoe UI", 13, "bold"),
                                bg="#888888", fg="#ffffff", anchor="w", padx=14, pady=10)
        self.hdr_lbl.pack(fill="x")

        body = tk.Frame(outer, bg=self.BODY_BG)
        body.pack(fill="both", expand=True)

        # readout: label column + value column, aligned
        rf = tk.Frame(body, bg=self.BODY_BG)
        rf.pack(fill="x", padx=16, pady=(12, 4))
        rf.columnconfigure(1, weight=1)
        for i, (key, label) in enumerate((("offset", "Offset"), ("server", "Server"),
                                          ("source", "Source"), ("lastsync", "Last sync"))):
            tk.Label(rf, text=label + ":", anchor="w", font=("Segoe UI", 10),
                     bg=self.BODY_BG, fg=self.LABEL_FG).grid(row=i, column=0, sticky="w", pady=2)
            v = tk.Label(rf, text="", anchor="w", font=("Segoe UI", 10),
                         bg=self.BODY_BG, fg=self.VALUE_FG)
            v.grid(row=i, column=1, sticky="w", padx=(14, 0), pady=2)
            self.rows[key] = v

        tk.Frame(body, bg=self.BORDER, height=1).pack(fill="x", padx=14, pady=(10, 8))

        # actions: uniform two-column button grid
        bf = tk.Frame(body, bg=self.BODY_BG)
        bf.pack(fill="x", padx=12, pady=(0, 12))
        bf.columnconfigure(0, weight=1, uniform="btn")
        bf.columnconfigure(1, weight=1, uniform="btn")

        def mkbtn(text, cmd, r, c):
            b = tk.Button(bf, text=text, command=cmd, font=("Segoe UI", 9))
            b.grid(row=r, column=c, sticky="ew", padx=3, pady=3)
            return b

        mkbtn("Refresh",
              lambda: threading.Thread(target=refresh_once, daemon=True).start(), 0, 0)
        mkbtn("Resync  (admin)", self._do_resync, 0, 1)
        mkbtn("Configure…  (admin)", self._do_configure, 1, 0)
        mkbtn("Open time.is", self._do_timeis, 1, 1)
        # Only meaningful while the service is stopped; hidden the rest of the time.
        self.btn_startsvc = mkbtn("Start service  (admin)", self._do_start_service, 2, 0)
        self.btn_startsvc.grid_remove()
        mkbtn("Close", self._hide, 2, 1)

        self.win.bind("<FocusOut>", self._on_focus_out)
        self.win.bind("<Escape>", lambda e: self._hide())

    def _show(self):
        if self.win is None:
            self._build()
        self._update()
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry("%dx%d+%d+%d" % (w, h, sw - w - 12, sh - h - 60))
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.focus_force()

    def _hide(self):
        if self.win is not None:
            self.win.withdraw()

    def _on_focus_out(self, _event):
        # Defer, then hide only if focus left the whole app (not an internal widget).
        self.win.after(120, self._maybe_hide)

    def _maybe_hide(self):
        try:
            focused = self.root.focus_get()
        except Exception:
            focused = None
        if focused is None:
            self._hide()

    def _do_resync(self):
        action_resync()

    def _do_start_service(self):
        from tkinter import messagebox
        # Offer the persistent fix, but never make it the silent default: a fresh
        # non-domain Windows leaves w32time on Manual/trigger start, which is the
        # usual reason the clock quietly stops syncing between reboots.
        set_auto = False
        if service_autostart() is False:
            set_auto = messagebox.askyesno(
                APP_NAME,
                "Start the Windows Time service now?\n\n"
                "It is currently set to start manually, so it may stop again "
                "after a reboot. Also set it to start automatically?\n\n"
                "Yes - start it and set it to start at boot\n"
                "No  - just start it this once\n\n"
                "Either way a UAC prompt will appear.",
                parent=self.win)
        action_start_service(set_auto)
        threading.Thread(target=refresh_once, daemon=True).start()

    def _do_timeis(self):
        webbrowser.open("https://time.is")

    def _do_configure(self):
        from tkinter import simpledialog, messagebox
        new = simpledialog.askstring(
            APP_NAME, "NTP server (IP or hostname):",
            initialvalue=state.cfg["server"], parent=self.win)
        if new:
            new = new.strip()
            if new != state.cfg["server"]:
                state.cfg["server"] = new
                save_config(state.cfg)
                state.server = new
            if messagebox.askyesno(
                    APP_NAME,
                    "Point Windows time service at %s now?\n\n"
                    "Needs administrator rights - a UAC prompt will appear." % new,
                    parent=self.win):
                action_configure_apply(new)
        threading.Thread(target=refresh_once, daemon=True).start()

    def notify_update(self, kind, latest=None, url=None, manual=False, err=None):
        """Thread-safe: report an update-check result to the user."""
        if self.root is not None:
            self.root.after(0, lambda: self._notify_update(kind, latest, url, manual, err))
        elif kind == "available" and state.icon is not None:
            try:
                state.icon.notify("Update available: %s" % latest, APP_NAME)
            except Exception:
                pass

    def _notify_update(self, kind, latest, url, manual, err):
        from tkinter import messagebox
        if kind == "available":
            if FROZEN and url:
                if messagebox.askyesno(
                        APP_NAME,
                        "A newer version is available.\n\n"
                        "Installed:  v%s\nLatest:     %s\n\n"
                        "Download and install it now?"
                        % (APP_VERSION, latest)):
                    threading.Thread(target=lambda: self_update(url, latest),
                                     daemon=True).start()
            elif messagebox.askyesno(
                    APP_NAME,
                    "A newer version is available.\n\n"
                    "Installed:  v%s\nLatest:     %s\n\n"
                    "Open the download page?" % (APP_VERSION, latest)):
                webbrowser.open(RELEASES_URL)
        elif kind == "uptodate" and manual:
            messagebox.showinfo(APP_NAME, "You're up to date (v%s)." % APP_VERSION)
        elif kind == "error" and manual:
            messagebox.showerror(APP_NAME, "Update check failed:\n%s" % err)
        elif kind == "installed":
            restart = messagebox.askyesno(
                APP_NAME,
                "Update installed (%s).\n\n"
                "Restart NTP Time Sync now to finish?\n"
                "(Choose No to finish later — it also starts at your next sign-in.)"
                % latest)
            if restart:
                _relaunch()
            state.stop.set()
            if state.icon is not None:
                state.icon.stop()
            self.close()
        elif kind == "update_failed":
            if messagebox.askyesno(
                    APP_NAME,
                    "Automatic update failed:\n%s\n\n"
                    "Open the download page instead?" % err):
                webbrowser.open(RELEASES_URL)

    def _update(self):
        if self.win is None:
            return
        color = state.color
        hexc = self._hexcolor(color)
        fg = "#1a1a1a" if color in ("yellow", "gray") else "#ffffff"
        self.hdr.configure(bg=hexc)
        self.hdr_lbl.configure(bg=hexc, fg=fg,
                               text="%s  —  %s" % (color.upper(), state.reason))
        self.rows["offset"].configure(text=state.offset_txt)
        self.rows["server"].configure(text=state.server)
        self.rows["source"].configure(text=state.source)
        self.rows["lastsync"].configure(text=state.lastsync_txt)
        if self.btn_startsvc is not None:
            if state.svc_up is False:
                self.btn_startsvc.grid()
            else:
                self.btn_startsvc.grid_remove()


panel = StatusPanel()


def on_show_panel(icon, item):
    panel.show()


def _version_tuple(s):
    out = []
    for part in s.lstrip("vV").strip().split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def check_updates(manual=False):
    """Query GitHub for the latest release; report via the panel. Runs in a thread."""
    import urllib.request
    import json
    try:
        req = urllib.request.Request(API_LATEST, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.load(resp)
    except Exception as e:
        panel.notify_update("error", err=str(e), manual=manual)
        return
    latest = data.get("tag_name", "")
    url = None
    for asset in data.get("assets", []):
        if asset.get("name", "").lower().endswith(".exe"):
            url = asset.get("browser_download_url")
            break
    if latest and _version_tuple(latest) > _version_tuple(APP_VERSION):
        panel.notify_update("available", latest=latest, url=url, manual=manual)
    else:
        panel.notify_update("uptodate", manual=manual)


def self_update(url, latest):
    """Download the new exe and swap it into place. Frozen builds only.

    Windows lets you rename a running .exe (just not overwrite it), so we rename
    the current exe aside and drop the freshly-downloaded one into its place.

    We do NOT relaunch here. Launching a just-written onefile exe races with
    antivirus scanning it, which reliably produces "failed to load Python DLL".
    On success we report "installed" and let the user opt into a restart (see
    _relaunch, which forces the AV scan to finish first). The app also starts at
    logon. The leftover .old.exe is cleaned up on next startup.
    """
    import urllib.request
    exe = sys.executable
    folder = os.path.dirname(exe)
    new = os.path.join(folder, "update.download.exe")
    old = os.path.join(folder, "update.old.exe")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=120) as resp:
            expected = resp.headers.get("Content-Length")
            data = resp.read()
        if expected is not None and len(data) != int(expected):
            raise ValueError("incomplete download: %d of %s bytes" % (len(data), expected))
        if len(data) < 1_000_000 or data[:2] != b"MZ":
            raise ValueError("downloaded file is not a valid executable")
        with open(new, "wb") as f:
            f.write(data)
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
        os.replace(exe, old)      # rename running exe aside (allowed on Windows)
        try:
            os.replace(new, exe)  # move new exe into the original path
        except Exception:
            os.replace(old, exe)  # restore original if the swap fails
            raise
    except Exception as e:
        try:
            if os.path.exists(new):
                os.remove(new)
        except OSError:
            pass
        panel.notify_update("update_failed", err=str(e))
        return
    panel.notify_update("installed", latest=latest)


def _relaunch():
    """Robustly relaunch the freshly-updated exe (only when the user opts in).

    Waits for THIS process to exit (so the onefile temp dir is cleaned up), then
    reads the new exe end-to-end so antivirus finishes scanning it BEFORE launch
    (that scan race is what broke the earlier auto-restarts), then launches it.
    """
    exe = sys.executable
    try:
        ps = ("$ErrorActionPreference='SilentlyContinue';"
              "Wait-Process -Id %d -Timeout 30;"
              "Get-FileHash -LiteralPath '%s' | Out-Null;"   # force AV to scan it now
              "Start-Sleep -Milliseconds 500;"
              "Start-Process -FilePath '%s'") % (os.getpid(), exe, exe)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass


def _cleanup_old_update():
    """Remove leftovers from a previous self-update."""
    if not FROZEN:
        return
    folder = os.path.dirname(sys.executable)
    for name in ("update.old.exe", "update.download.exe", "relaunch.cmd"):
        p = os.path.join(folder, name)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _startup_autocheck():
    if not state.stop.wait(3):          # small delay; skip if quitting
        check_updates(manual=False)


def on_check_updates(icon, item):
    threading.Thread(target=lambda: check_updates(manual=True), daemon=True).start()


def on_toggle_autocheck(icon, item):
    state.cfg["auto_check_updates"] = not state.cfg.get("auto_check_updates", False)
    save_config(state.cfg)


def on_toggle_logon(icon, item):
    set_logon(not logon_enabled())


def on_open_config_dir(icon, item):
    # Opens Explorer right at the config folder. For the installed app CONFIG_DIR
    # lives under %APPDATA%, which is hidden by default and most users can't
    # navigate to by hand -- but a direct open lands there regardless of the
    # hidden-files setting, so they never need to know the path.
    try:
        os.startfile(CONFIG_DIR)          # noqa: S606 - Windows-only, trusted path
    except Exception:
        pass


def on_install(icon, item):
    if do_install():
        on_quit(icon, item)               # installed copy is launching; exit this one


def on_uninstall(icon, item):
    # Spawn a separate --uninstall invocation; it confirms, then its trampoline
    # stops this tray and deletes the install dir.
    try:
        subprocess.Popen([INSTALL_EXE, "--uninstall"])
    except Exception:
        pass


def build_menu():
    # Everything lives in the panel now; the native right-click menu is a minimal
    # fallback (left-click opens the panel via the default item). The startup and
    # install/uninstall items depend on how the app is running.
    items = [
        pystray.MenuItem("Open NTP Time Sync", on_show_panel, default=True),
        pystray.Menu.SEPARATOR,
    ]
    if IS_LOOSE:
        items.append(pystray.MenuItem("Install NTP Time Sync…", on_install))
    if IS_INSTALLED or not FROZEN:
        # A loose or portable run must not pin the Run key to a Downloads/USB path.
        items.append(pystray.MenuItem("Start at logon", on_toggle_logon,
                                      checked=lambda item: logon_enabled()))
    items += [
        pystray.MenuItem("Check for updates", on_check_updates),
        pystray.MenuItem("Auto-check on startup", on_toggle_autocheck,
                         checked=lambda item: state.cfg.get("auto_check_updates", False)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open settings folder", on_open_config_dir),
    ]
    if IS_INSTALLED:
        items.append(pystray.MenuItem("Uninstall…", on_uninstall))
    items.append(pystray.MenuItem("Quit", on_quit))
    return pystray.Menu(*items)


def _wait_for_shell(timeout=90):
    """Block until the taskbar's notification area exists.

    A Run-key app launched at logon can start before Explorer has created the
    tray. Adding an icon then fails silently and the app exits with no dot --
    which is why it worked on a manual launch but not at logon. Waiting for
    Shell_TrayWnd closes that race. No-op once the shell is up (manual launch).
    """
    try:
        find = ctypes.windll.user32.FindWindowW
    except Exception:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find("Shell_TrayWnd", None):
            return True
        time.sleep(1)
    return False


def _startup_log(msg):
    """Append a timestamped breadcrumb to CONFIG_DIR. Unconditional (not just on
    error) so a logon start that produces no dot can be traced: whether main()
    ran at all, whether the shell was ready, and whether the tray loop exited."""
    try:
        with open(os.path.join(CONFIG_DIR, "startup.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (dt.datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


_singleton_handle = None


def _acquire_singleton():
    """False if another instance already holds the named mutex. Guards against a
    second tray icon if more than one launch path ever fires (e.g. a lingering
    Run-key value plus the Startup shortcut). Keeps a module-level ref so the
    handle lives for the whole process."""
    global _singleton_handle
    try:
        k32 = ctypes.windll.kernel32
        _singleton_handle = k32.CreateMutexW(None, False, "Local\\NtpTimeSync_singleton")
        return k32.GetLastError() != 183          # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _log_startup_error():
    """Append a traceback to CONFIG_DIR so a silent windowed-app crash at logon
    leaves evidence instead of just vanishing."""
    import traceback
    try:
        with open(os.path.join(CONFIG_DIR, "startup-error.log"), "a",
                  encoding="utf-8") as f:
            f.write("---- %s (v%s) ----\n" % (dt.datetime.now().isoformat(), APP_VERSION))
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def main():
    if "--uninstall" in sys.argv[1:]:
        do_uninstall()
        return
    if "--install" in sys.argv[1:]:
        do_install()                  # silent install (no prompt); then relaunched
        return

    _startup_log("main start: INSTALLED=%s LOOSE=%s PORTABLE=%s exe=%s"
                 % (IS_INSTALLED, IS_LOOSE, IS_PORTABLE, sys.executable))
    _cleanup_old_update()

    if IS_LOOSE:
        # Bare exe (e.g. straight from Downloads): offer to install itself. If it
        # installs, the installed copy is now launching, so this one bows out.
        if offer_install():
            return

    if not _acquire_singleton():
        _startup_log("another instance already running; exiting")
        return

    if IS_INSTALLED:
        reconcile_logon()             # clear legacy Run key; ensure Startup shortcut
        refresh_uninstall_entry()     # keep the Add/Remove version current

    panel.start()
    threading.Thread(target=poll_loop, daemon=True).start()
    if state.cfg.get("auto_check_updates", False):
        threading.Thread(target=_startup_autocheck, daemon=True).start()

    # At logon the notification area may not exist yet (or may not accept the
    # icon on the first try). Wait for the shell, then create the icon; if
    # pystray's loop returns without us asking to quit, the add likely failed --
    # rebuild and retry a few times rather than exit dot-less.
    _startup_log("shell ready=%s" % _wait_for_shell())
    for attempt in range(1, 6):
        state.icon = pystray.Icon(
            "ntp_time_sync", make_icon(state.color), state.tooltip(), build_menu())
        _startup_log("tray attempt %d -> run()" % attempt)
        try:
            state.icon.run()
        except Exception:
            _log_startup_error()
            _startup_log("tray attempt %d raised" % attempt)
        if state.stop.is_set():
            _startup_log("quit requested; exiting")
            return
        _startup_log("run() returned without quit; retrying in 3s")
        time.sleep(3)
    _startup_log("gave up after 5 tray attempts")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log_startup_error()
        raise
