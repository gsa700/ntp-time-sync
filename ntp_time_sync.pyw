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
APP_VERSION = "1.3.7"
REPO = "gsa700/ntp-time-sync"
RELEASES_URL = "https://github.com/%s/releases/latest" % REPO
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
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
    "auto_check_updates": False,  # check GitHub for a newer release at startup
    "logon_initialized": False,   # first run enables Start-at-logon by default
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
    """Return a dict of display fields + the traffic-light color.

    The light is driven by the measured clock offset (real accuracy) against the
    configured reference server, so it works no matter which NTP server Windows
    itself uses. A source that has fallen back to the free-running CMOS clock is
    flagged even when the offset still looks OK. Requiring Windows to sync to
    this exact server is opt-in via cfg["require_server"].
    """
    st = query_status()
    server = cfg["server"]
    offset, reachable = probe_offset(server)
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
            tk.Button(bf, text=text, command=cmd, font=("Segoe UI", 9)).grid(
                row=r, column=c, sticky="ew", padx=3, pady=3)

        mkbtn("Refresh",
              lambda: threading.Thread(target=refresh_once, daemon=True).start(), 0, 0)
        mkbtn("Resync  (admin)", self._do_resync, 0, 1)
        mkbtn("Configure…  (admin)", self._do_configure, 1, 0)
        mkbtn("Open time.is", self._do_timeis, 1, 1)
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
                        "Download and install it now? The app will restart."
                        % (APP_VERSION, latest)):
                    threading.Thread(target=lambda: self_update(url), daemon=True).start()
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


def self_update(url):
    """Download the new exe, swap it into place, and relaunch. Frozen builds only.

    Windows lets you rename a running .exe (just not overwrite it), so we rename
    the current exe aside, drop the freshly-downloaded one into its place, launch
    it, and quit. The leftover .old.exe is cleaned up on next startup.
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
    # Relaunch via a tiny detached helper so THIS process fully exits (and lets
    # PyInstaller remove its onefile temp dir) before the new instance starts.
    # Launching the exe directly from the dying process triggers a spurious
    # "failed to remove temporary directory" dialog on onefile builds.
    # Relaunch via a hidden PowerShell: Wait-Process reliably blocks until THIS
    # process exits (so the onefile temp dir is cleaned up first), then a short
    # settle lets antivirus finish scanning the fresh exe before Start-Process
    # launches it. Far more robust than a batch tasklist loop.
    try:
        ps = ("$ErrorActionPreference='SilentlyContinue';"
              "Wait-Process -Id %d -Timeout 30;"
              "Start-Sleep -Seconds 3;"
              "Start-Process -FilePath '%s'") % (os.getpid(), exe)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass
    state.stop.set()
    if state.icon is not None:
        state.icon.stop()
    panel.close()


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


def build_menu():
    # Everything lives in the panel now; the native right-click menu is a minimal
    # fallback (left-click opens the panel via the default item).
    return pystray.Menu(
        pystray.MenuItem("Open NTP Time Sync", on_show_panel, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start at logon", on_toggle_logon,
                         checked=lambda item: logon_enabled()),
        pystray.MenuItem("Check for updates", on_check_updates),
        pystray.MenuItem("Auto-check on startup", on_toggle_autocheck,
                         checked=lambda item: state.cfg.get("auto_check_updates", False)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )


def main():
    _cleanup_old_update()
    panel.start()
    if not state.cfg.get("logon_initialized", False):
        set_logon(True)                       # default Start-at-logon ON, first run only
        state.cfg["logon_initialized"] = True
        save_config(state.cfg)
    state.icon = pystray.Icon(
        "ntp_time_sync", make_icon("gray"),
        "%s\nstarting..." % APP_NAME, build_menu())
    threading.Thread(target=poll_loop, daemon=True).start()
    if state.cfg.get("auto_check_updates", False):
        threading.Thread(target=_startup_autocheck, daemon=True).start()
    state.icon.run()


if __name__ == "__main__":
    main()
