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
import threading
import subprocess
import webbrowser
import datetime as dt
import ctypes

from PIL import Image, ImageDraw
import pystray

APP_NAME = "NTP Time Sync"
FROZEN = getattr(sys, "frozen", False)

# When bundled to an .exe (PyInstaller), the program may live in a read-only
# location (Program Files) and __file__ points into a temp extraction dir, so
# keep config in %APPDATA%. When run as a script, keep it next to the script.
if FROZEN:
    CONFIG_DIR = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
else:
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
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


def probe_offset(server):
    """Return (offset_seconds or None, reachable bool)."""
    if not server:
        return (None, False)
    try:
        p = _run(["w32tm", "/stripchart", "/computer:%s" % server,
                  "/samples:1", "/dataonly"], timeout=12)
        text = (p.stdout or "") + (p.stderr or "")
        if "error" in text.lower():
            return (None, False)
        matches = re.findall(r"([+-]?\d+\.\d+)s\b", text)
        if not matches:
            return (None, False)
        return (float(matches[-1]), True)
    except Exception:
        return (None, False)


# --------------------------------------------------------------- evaluation

def evaluate(cfg):
    """Return a dict of display fields + the traffic-light color."""
    st = query_status()
    server = cfg["server"]
    offset, reachable = probe_offset(server)
    source = st["source"] or ""
    source_ok = bool(server) and server in source

    age_min = None
    if st["last_sync"] is not None:
        age_min = (dt.datetime.now() - st["last_sync"]).total_seconds() / 60.0
        stale = age_min > cfg["stale_minutes"]
    else:
        stale = True

    if not reachable or offset is None:
        color, reason = "red", "%s unreachable" % server
    elif not source_ok:
        color = "red"
        reason = "source is %s, not %s" % (source or "none", server)
    else:
        a = abs(offset)
        if a <= cfg["green_max_offset"] and not stale:
            color, reason = "green", "healthy"
        elif a <= cfg["yellow_max_offset"] or stale:
            color = "yellow"
            reason = "drifting" if a > cfg["green_max_offset"] else "sync stale"
        else:
            color, reason = "red", "offset %+.3f s" % offset

    offset_txt = ("%+.3f s" % offset) if offset is not None else "unreachable"
    if st["last_sync"]:
        lastsync_txt = st["last_sync"].strftime("%m/%d %H:%M:%S")
        if age_min is not None:
            lastsync_txt += "  (%.0f min ago)" % age_min
    else:
        lastsync_txt = "n/a"

    return {
        "color": color, "reason": reason, "server": server,
        "offset_txt": offset_txt, "source": source or "n/a",
        "lastsync_txt": lastsync_txt,
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
    run_elevated_ps("w32tm /resync /force; w32tm /query /status")


def action_configure_apply(server):
    ps = ("w32tm /config /manualpeerlist:'%s,0x8' /syncfromflags:manual /update; "
          "Restart-Service w32time; "
          "w32tm /resync /force; "
          "w32tm /query /status" % server)
    run_elevated_ps(ps)


# --------------------------------------------------------------- start at logon

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "NtpTimeSync"


def _logon_command():
    """Command to relaunch at logon: the exe itself when frozen, else pythonw + script."""
    if FROZEN:
        return '"%s"' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def logon_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_logon(enable):
    import winreg
    cmd = _logon_command()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enable:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


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
        self.icon = None
        self.stop = threading.Event()

    def apply(self, fields):
        self.color = fields["color"]
        self.reason = fields["reason"]
        self.server = fields["server"]
        self.offset_txt = fields["offset_txt"]
        self.source = fields["source"]
        self.lastsync_txt = fields["lastsync_txt"]

    def tooltip(self):
        return "%s\n%s  %s\n%s" % (
            APP_NAME, self.color.upper(), self.offset_txt, self.server)


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


def poll_loop():
    while not state.stop.is_set():
        refresh_once()
        state.stop.wait(state.cfg["poll_seconds"])


# --------------------------------------------------------------- menu handlers

def on_refresh(icon, item):
    threading.Thread(target=refresh_once, daemon=True).start()


def on_resync(icon, item):
    action_resync()


def on_configure(icon, item):
    threading.Thread(target=_configure_dialog, daemon=True).start()


def _configure_dialog():
    import tkinter as tk
    from tkinter import simpledialog, messagebox
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    new = simpledialog.askstring(
        APP_NAME, "NTP server (IP or hostname):",
        initialvalue=state.cfg["server"], parent=root)
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
                parent=root):
            action_configure_apply(new)
    root.destroy()
    threading.Thread(target=refresh_once, daemon=True).start()


def on_timeis(icon, item):
    webbrowser.open("https://time.is")


def on_toggle_logon(icon, item):
    set_logon(not logon_enabled())


def on_quit(icon, item):
    state.stop.set()
    icon.stop()


def build_menu():
    return pystray.Menu(
        pystray.MenuItem(lambda item: "●  %s: %s" % (state.color.upper(), state.reason),
                         on_refresh),
        pystray.MenuItem(lambda item: "   Offset: %s" % state.offset_txt,
                         on_refresh),
        pystray.MenuItem(lambda item: "   Server: %s" % state.server,
                         on_refresh),
        pystray.MenuItem(lambda item: "   Source: %s" % state.source,
                         on_refresh),
        pystray.MenuItem(lambda item: "   Last sync: %s" % state.lastsync_txt,
                         on_refresh),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh now", on_refresh, default=True),
        pystray.MenuItem("Resync clock  (admin)", on_resync),
        pystray.MenuItem("Configure server...  (admin)", on_configure),
        pystray.MenuItem("Open time.is", on_timeis),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start at logon", on_toggle_logon,
                         checked=lambda item: logon_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )


def main():
    state.icon = pystray.Icon(
        "ntp_time_sync", make_icon("gray"),
        "%s\nstarting..." % APP_NAME, build_menu())
    threading.Thread(target=poll_loop, daemon=True).start()
    state.icon.run()


if __name__ == "__main__":
    main()
