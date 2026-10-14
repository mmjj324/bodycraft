# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""One-step install: a desktop icon, plus starting things automatically when you log in.

What it creates, per system (uninstall.py removes exactly these):

Windows
  - Desktop shortcut "Bodycraft.lnk" that runs start.py
  - Startup shortcuts "Bodycraft Server.lnk" and "Bodycraft Reminder Bar.lnk"
  The Desktop and Startup folders come from PowerShell's GetFolderPath. When OneDrive takes over
  the desktop, ~/Desktop is an empty shell, and a shortcut put there lands somewhere you never see.
  Shortcut arguments are single-quote escaped, so a ' in a path can't break the PowerShell script.

macOS
  - ~/Desktop/Bodycraft.command, a double-clickable launcher that runs start.py
  - optional: ~/Library/LaunchAgents/local.bodycraft.server.plist, starts the server at login

Linux
  - ~/.local/share/applications/bodycraft.desktop (shows up in your app menu)
  - a copy on ~/Desktop, if you have a Desktop folder
  - optional: ~/.config/autostart/bodycraft-server.desktop, starts the server at login

Nothing opens a browser window at login. A window that pops up on its own every time you start
your computer is exactly the kind of thing people end up switching off.
"""
import os
import sys
import base64
import shlex
import shutil
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
WIDGET = os.path.join(BASE, "bodycraft_widget.py")
SERVER = os.path.join(BASE, "bodycraft_server.py")
OPENER = os.path.join(BASE, "start.py")
ICON_ICO = os.path.join(BASE, "bodycraft.ico")
ICON_PNG = os.path.join(BASE, "bodycraft.png")
SERVER_LOG = os.path.join(BASE, ".server.log")

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# Every file we write on macOS/Linux carries this marker, so uninstall.py only removes our own files.
MARKER = "Created by Bodycraft install.py"

# Names shared with uninstall.py -- keep the two in sync.
WIN_DESKTOP_LNK = "Bodycraft.lnk"
WIN_STARTUP_SERVER_LNK = "Bodycraft Server.lnk"
WIN_STARTUP_WIDGET_LNK = "Bodycraft Reminder Bar.lnk"
MAC_COMMAND = "Bodycraft.command"
MAC_AGENT_LABEL = "local.bodycraft.server"
LINUX_DESKTOP = "bodycraft.desktop"
LINUX_AUTOSTART = "bodycraft-server.desktop"


# ======================================================================
# Windows
# ======================================================================

def run_ps(script):
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


def ps_quote(value):
    """Inside a PowerShell single-quoted string, ' must be written as '' or it ends the string."""
    return str(value).replace("'", "''")


def win_shell_folders():
    """The real Desktop and Startup folders (handles OneDrive redirection)."""
    r = run_ps("[Environment]::GetFolderPath('Desktop')\n"
               "[Environment]::GetFolderPath('Startup')")
    lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    home = os.path.expanduser("~")
    desktop = lines[0] if len(lines) > 0 else os.path.join(home, "Desktop")
    startup = lines[1] if len(lines) > 1 else os.path.join(
        home, "AppData", "Roaming", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return desktop, startup


def find_pythonw():
    """A Python that has tkinter (the reminder bar needs it). Prefers pythonw.exe: no console window."""
    cands = []
    d = os.path.dirname(sys.executable)
    cands += [os.path.join(d, "pythonw.exe"), os.path.join(d, "python.exe")]
    for name in ("pythonw.exe", "python.exe", "python"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    cands.append(sys.executable)
    for p in cands:
        if not p or not os.path.exists(p):
            continue
        try:
            r = subprocess.run([p, "-c", "import tkinter"], capture_output=True)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return None


def make_lnk(lnk_path, target, args, workdir, icon=None):
    lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        "$lnk = $ws.CreateShortcut('%s')" % ps_quote(lnk_path),
        "$lnk.TargetPath = '%s'" % ps_quote(target),
        "$lnk.Arguments = '%s'" % ps_quote(args),
        "$lnk.WorkingDirectory = '%s'" % ps_quote(workdir),
    ]
    if icon:
        lines.append("$lnk.IconLocation = '%s'" % ps_quote(icon))
    lines.append("$lnk.Save()")
    r = run_ps("\n".join(lines))
    # don't trust the return code alone; check that the file really exists
    return r.returncode == 0 and os.path.exists(lnk_path)


def win_launch(py_script, pythonw):
    flags = 0x00000008 | 0x00000200 | 0x01000000
    try:
        subprocess.Popen([pythonw, py_script], creationflags=flags, close_fds=True, cwd=BASE)
        return True
    except Exception:
        try:
            subprocess.Popen([pythonw, py_script], creationflags=0x00000008,
                             close_fds=True, cwd=BASE)
            return True
        except Exception:
            return False


def install_windows():
    pyw = find_pythonw()
    if not pyw:
        print("[X] Couldn't find a Python with tkinter. Please install Python from python.org")
        print("    (leave \"tcl/tk and IDLE\" checked in the installer), then run this again.")
        return

    desktop, startup = win_shell_folders()
    print("    Desktop folder:", desktop)
    print("    Startup folder:", startup)

    # Use the .ico if it's there; otherwise fall back to Python's icon. A missing icon
    # shouldn't make the install fail.
    icon = ICON_ICO if os.path.exists(ICON_ICO) else None
    p1 = os.path.join(desktop, WIN_DESKTOP_LNK)
    ok1 = make_lnk(p1, pyw, '"%s"' % OPENER, BASE, icon)
    print("[1] Desktop icon            :", "OK  " + p1 if ok1 else "failed")

    p2 = os.path.join(startup, WIN_STARTUP_SERVER_LNK)
    ok2 = make_lnk(p2, pyw, '"%s"' % SERVER, BASE)
    print("[2] Start server at login   :", "OK  " + p2 if ok2 else "failed")

    p3 = os.path.join(startup, WIN_STARTUP_WIDGET_LNK)
    ok3 = make_lnk(p3, pyw, '"%s"' % WIDGET, BASE, icon)
    print("[3] Start reminder bar at login:", "OK  " + p3 if ok3 else "failed")

    print("[4] Start the server now    :", "OK" if win_launch(SERVER, pyw) else "failed")
    print("[5] Start the reminder bar  :", "OK" if win_launch(WIDGET, pyw) else "failed")

    if not (ok1 and ok2 and ok3):
        print("\nSome shortcuts couldn't be created. You can make one by hand: right-click start.py,")
        print("choose \"Send to > Desktop (create shortcut)\". Everything else still works.")
        print("To uninstall later, run uninstall.py.")

    print("\n=== Done. Double-click \"Bodycraft\" on your desktop to get started. ===")
    print("Only the background server and the reminder bar start at login. No browser window pops up.")


# ======================================================================
# macOS / Linux shared bits
# ======================================================================

def ask_yes_no(question):
    try:
        ans = input(question + " [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def write_text(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


def python_exe():
    return os.path.abspath(sys.executable)


def posix_start_server():
    """Start the server now, detached, with output going to .server.log."""
    try:
        log = open(SERVER_LOG, "ab")
    except Exception:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen([python_exe(), SERVER], cwd=BASE, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
        return True
    except Exception:
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


# ======================================================================
# macOS
# ======================================================================

def mac_paths():
    home = os.path.expanduser("~")
    return (os.path.join(home, "Desktop", MAC_COMMAND),
            os.path.join(home, "Library", "LaunchAgents", MAC_AGENT_LABEL + ".plist"))


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def install_mac():
    command_path, plist_path = mac_paths()

    # [1] a double-clickable launcher on the Desktop
    script = (
        "#!/bin/sh\n"
        "# %s\n"
        "# Double-click to open Bodycraft. It's safe to delete this file;\n"
        "# uninstall.py removes it for you.\n"
        "cd %s && exec %s %s\n"
    ) % (MARKER, shlex.quote(BASE), shlex.quote(python_exe()), shlex.quote(OPENER))
    try:
        write_text(command_path, script, 0o755)
        print("[1] Desktop launcher        : OK  " + command_path)
    except Exception as e:
        print("[1] Desktop launcher        : failed (%s)" % e)

    # [2] optional: start the server at login
    print()
    print("    Bodycraft can start its small background server when you log in, so the")
    print("    reminder bar and the app are ready right away. It only listens on this computer.")
    if ask_yes_no("    Start the server automatically at login?"):
        plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- {marker} -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{server}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{base}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
""".format(marker=MARKER, label=MAC_AGENT_LABEL, py=xml_escape(python_exe()),
           server=xml_escape(SERVER), base=xml_escape(BASE), log=xml_escape(SERVER_LOG))
        # No KeepAlive on purpose: start.py restarts the server after an upgrade by starting
        # a new one, which stops the old one. If launchd kept reviving the old one, they'd fight.
        try:
            write_text(plist_path, plist, 0o644)
            print("[2] Start server at login   : OK  " + plist_path)
            print("    (takes effect the next time you log in)")
            home = os.path.expanduser("~")
            protected = [os.path.join(home, x) for x in ("Desktop", "Documents", "Downloads")]
            if any(BASE == p or BASE.startswith(p + os.sep) for p in protected):
                print("    Note: this folder is inside Desktop, Documents or Downloads. macOS privacy")
                print("    settings may stop login items from reading it. If the server doesn't start")
                print("    at login, move the Bodycraft folder to your home folder and run install.py again.")
        except Exception as e:
            print("[2] Start server at login   : failed (%s)" % e)
    else:
        print("[2] Start server at login   : skipped")

    print("[3] Start the server now    :", "OK" if posix_start_server() else "failed")

    print("\n=== Done. Double-click \"Bodycraft.command\" on your Desktop to get started. ===")
    print("The first time, macOS may ask whether to open it: right-click it and choose Open.")
    print("A Terminal window opens while it starts; you can close it once Bodycraft is up.")
    print("Want the desktop reminder bar? Run widget_toggle.py.")


# ======================================================================
# Linux
# ======================================================================

def linux_paths():
    home = os.path.expanduser("~")
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return (os.path.join(data_home, "applications", LINUX_DESKTOP),
            os.path.join(config_home, "autostart", LINUX_AUTOSTART))


def linux_desktop_dir():
    """The user's Desktop folder, if there is one (respects localized names via xdg-user-dir)."""
    try:
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5)
        d = (r.stdout or "").strip()
        if d and os.path.isdir(d) and os.path.abspath(d) != os.path.expanduser("~"):
            return d
    except Exception:
        pass
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    return d if os.path.isdir(d) else None


def desktop_escape(s):
    """Escape a value for a .desktop string key (backslash is the escape character)."""
    return str(s).replace("\\", "\\\\").replace("\n", "\\n")


def desktop_exec_arg(arg):
    """Quote one argument for an Exec= line, per the Desktop Entry spec."""
    s = str(arg)
    for ch in ("\\", '"', "`", "$"):
        s = s.replace(ch, "\\" + ch)
    s = '"' + s + '"'
    # Exec= is also a string value (one more level of backslash escaping), and % is a field code
    return s.replace("\\", "\\\\").replace("%", "%%")


def desktop_entry(name, comment, args, icon=None, extra=""):
    lines = [
        "[Desktop Entry]",
        "# " + MARKER,
        "Type=Application",
        "Version=1.0",
        "Name=" + name,
        "Comment=" + comment,
        "Exec=" + " ".join(desktop_exec_arg(a) for a in args),
        "Path=" + desktop_escape(BASE),
        "Terminal=false",
    ]
    if icon:
        lines.append("Icon=" + desktop_escape(icon))
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n"


def install_linux():
    app_path, autostart_path = linux_paths()
    icon = ICON_PNG if os.path.exists(ICON_PNG) else None

    # [1] app menu entry
    entry = desktop_entry("Bodycraft", "Your daily routine and fat-loss tracker",
                          [python_exe(), OPENER], icon,
                          "Categories=Utility;\nStartupNotify=false")
    try:
        write_text(app_path, entry, 0o755)
        print("[1] App menu entry          : OK  " + app_path)
    except Exception as e:
        print("[1] App menu entry          : failed (%s)" % e)

    # [2] a copy on the Desktop, if there is one
    desk = linux_desktop_dir()
    if desk:
        desk_path = os.path.join(desk, LINUX_DESKTOP)
        try:
            write_text(desk_path, entry, 0o755)
            # GNOME only runs desktop icons that are marked trusted; harmless elsewhere
            try:
                subprocess.run(["gio", "set", desk_path, "metadata::trusted", "true"],
                               capture_output=True, timeout=5)
            except Exception:
                pass
            print("[2] Desktop icon            : OK  " + desk_path)
            print("    (if your desktop shows it as a text file, right-click it and choose \"Allow Launching\")")
        except Exception as e:
            print("[2] Desktop icon            : failed (%s)" % e)
    else:
        print("[2] Desktop icon            : skipped (no Desktop folder found)")

    # [3] optional: start the server at login
    print()
    print("    Bodycraft can start its small background server when you log in, so the")
    print("    reminder bar and the app are ready right away. It only listens on this computer.")
    if ask_yes_no("    Start the server automatically at login?"):
        auto = desktop_entry("Bodycraft Server", "Starts the Bodycraft local server",
                             [python_exe(), SERVER], None,
                             "NoDisplay=true\nX-GNOME-Autostart-enabled=true")
        try:
            write_text(autostart_path, auto, 0o644)
            print("[3] Start server at login   : OK  " + autostart_path)
        except Exception as e:
            print("[3] Start server at login   : failed (%s)" % e)
    else:
        print("[3] Start server at login   : skipped")

    print("[4] Start the server now    :", "OK" if posix_start_server() else "failed")

    print("\n=== Done. Open \"Bodycraft\" from your app menu or desktop to get started. ===")
    if not icon:
        print("Tip: run make_icon.py (needs Pillow) and then install.py again to get an app icon.")
    print("Want the desktop reminder bar? Run widget_toggle.py (needs python3-tk).")


# ======================================================================

def main():
    print("=== Bodycraft · Install ===")
    print("    by @mmjj324")
    print("    Folder:", BASE)
    if IS_WIN:
        install_windows()
    elif IS_MAC:
        install_mac()
    else:
        install_linux()
    print("To remove these shortcuts later, run uninstall.py. Your data stays in this folder.")


if __name__ == "__main__":
    main()
