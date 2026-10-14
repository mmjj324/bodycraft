# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""Uninstall: stop the background processes, remove the shortcuts and login items that install.py
created, then ask whether you also want your data cleared.

Works on Windows, macOS and Linux, and removes only what install.py creates on that system.

Your data is kept by default. That can catch people off guard: if you keep it and then put a new
version in the same folder, it picks up the old content.json / data.json / api_config.json as-is.
It won't ask for your goal weight again and your API key is still there. That looks like "it didn't
uninstall cleanly", but it's by design. To start from zero, choose to clear the data when asked.
Even then nothing is deleted outright: it's all moved into a time-stamped backup folder first.
"""
import os
import sys
import json
import time
import shutil
import base64
import datetime
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
RUN_FLAG = os.path.join(BASE, ".server_running.json")
WIDGET_FLAG = os.path.join(BASE, ".widget_running.json")
SERVER = os.path.join(BASE, "bodycraft_server.py")
WIDGET = os.path.join(BASE, "bodycraft_widget.py")

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# Must match install.py
MARKER = "Created by Bodycraft install.py"
WIN_DESKTOP = ["Bodycraft.lnk"]
WIN_STARTUP = ["Bodycraft Server.lnk", "Bodycraft Reminder Bar.lnk", "Bodycraft.lnk"]
MAC_COMMAND = "Bodycraft.command"
MAC_AGENT_LABEL = "local.bodycraft.server"
LINUX_DESKTOP = "bodycraft.desktop"
LINUX_AUTOSTART = "bodycraft-server.desktop"

DATA_FILES = ["data.json", "content.json", "diary.json", "photos.json", "selfies.json",
              "attach.json", "knowledge.json", "food_lookup.json", "favorites.json",
              "member_setup.json", "api_config.json", "api_key.txt",
              "photos", "selfies", "files", "backups"]


# ======================================================================
# Windows helpers
# ======================================================================

def run_ps(script):
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


def win_shell_folders():
    r = run_ps("[Environment]::GetFolderPath('Desktop')\n"
               "[Environment]::GetFolderPath('Startup')")
    lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    home = os.path.expanduser("~")
    desktop = lines[0] if len(lines) > 0 else os.path.join(home, "Desktop")
    startup = lines[1] if len(lines) > 1 else os.path.join(
        home, "AppData", "Roaming", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return desktop, startup


def _tasklist(pid):
    r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                       capture_output=True)
    return r.stdout.decode("latin-1", "ignore").lower()


# ======================================================================
# Processes
# ======================================================================

def _posix_command(pid):
    """The full command line of a process, or "" if it's gone."""
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _is_python(pid):
    if IS_WIN:
        return "python" in _tasklist(pid)
    return "python" in _posix_command(pid).lower()


def _alive(pid):
    if IS_WIN:
        return "python" in _tasklist(pid)
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    # an exited process waiting to be reaped (a zombie) still answers kill(pid, 0)
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                           capture_output=True, text=True, timeout=5)
        stat = (r.stdout or "").strip()
        return bool(stat) and not stat.startswith("Z")
    except Exception:
        return True


def _kill(pid, label):
    """Ask nicely first, then force it.

    The identity check comes first: the PID comes from a run flag this app wrote itself, and we
    also confirm it's a Python process. That check is the safety boundary. Forcing is still needed:
    on Windows, a windowless pythonw ignores a plain taskkill (it has no window to receive the close
    message), so without /F the server and reminder bar would keep running after uninstall, with this
    folder as their working directory, and Windows would refuse to delete it ("in use").
    """
    if not _is_python(pid):
        print("    %s: PID %d isn't a Python process, skipping it (won't touch other programs)" % (label, pid))
        return True

    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
        time.sleep(1.2)
        if _alive(pid):
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
            time.sleep(0.8)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                if not _alive(pid):
                    break
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.2)
        except Exception:
            pass
    ok = not _alive(pid)
    print("    %s: %s (PID %d)" % (label, "stopped" if ok else "could not stop", pid))
    return ok


def stop_by_flag(flag_path, label):
    """Only stop the PID recorded in our own run flag. Never scan by port or process name."""
    try:
        with open(flag_path, "r", encoding="utf-8") as f:
            pid = int(json.load(f).get("pid") or 0)
    except Exception:
        print("    %s: not running" % label)
        return True
    if not pid or pid == os.getpid():
        return True
    if not _alive(pid):
        print("    %s: not running" % label)
        ok = True
    else:
        ok = _kill(pid, label)
    try:
        os.remove(flag_path)
    except Exception:
        pass
    return ok


def find_leftovers():
    """Fallback: find Bodycraft processes whose run flag is gone.

    The flag may have been deleted, or the process was started by an older copy, so the flag
    can't find it, but it still holds this folder. We match on the command line: a Python process
    running this folder's server or reminder bar script (full path). It's still an identity check,
    not a sweep by port or process name.
    """
    targets = [SERVER, WIDGET]
    me = {os.getpid(), os.getppid()}
    pids = []
    if IS_WIN:
        cond = " -or ".join("$_.CommandLine -match [regex]::Escape('%s')" % t.replace("'", "''")
                            for t in targets)
        ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" |"
              " Where-Object { " + cond + " } | ForEach-Object { $_.ProcessId }")
        out = run_ps(ps).stdout or ""
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    else:
        try:
            out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                 text=True, timeout=10).stdout or ""
        except Exception:
            out = ""
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            cmd = parts[1]
            if "python" in cmd.lower() and any(t in cmd for t in targets):
                pids.append(int(parts[0]))
    return [p for p in pids if p not in me]


# ======================================================================
# Shortcuts and login items
# ======================================================================

def _remove(path):
    try:
        os.remove(path)
        print("    Removed", path)
        return True
    except Exception as e:
        print("    Couldn't remove", path, "-", e)
        return False


def _remove_if_ours(path):
    """Remove a text file only if install.py wrote it (it carries our marker line)."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            ours = MARKER in f.read()
    except Exception:
        ours = False
    if not ours:
        print("    Left alone (not created by Bodycraft):", path)
        return False
    return _remove(path)


def remove_shortcuts_windows():
    desktop, startup = win_shell_folders()
    removed = 0
    for folder, names in ((desktop, WIN_DESKTOP), (startup, WIN_STARTUP)):
        for name in names:
            p = os.path.join(folder, name)
            if os.path.exists(p) and _remove(p):
                removed += 1
    return removed


def remove_shortcuts_mac():
    home = os.path.expanduser("~")
    removed = 0
    if _remove_if_ours(os.path.join(home, "Desktop", MAC_COMMAND)):
        removed += 1
    plist = os.path.join(home, "Library", "LaunchAgents", MAC_AGENT_LABEL + ".plist")
    if os.path.exists(plist):
        # unregister it from launchd if it's loaded; harmless if it isn't
        try:
            subprocess.run(["launchctl", "unload", plist], capture_output=True, timeout=10)
        except Exception:
            pass
        if _remove_if_ours(plist):
            removed += 1
    return removed


def _linux_desktop_dirs():
    dirs = []
    try:
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5)
        d = (r.stdout or "").strip()
        if d and os.path.isdir(d) and os.path.abspath(d) != os.path.expanduser("~"):
            dirs.append(d)
    except Exception:
        pass
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(d) and d not in dirs:
        dirs.append(d)
    return dirs


def remove_shortcuts_linux():
    home = os.path.expanduser("~")
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    paths = [os.path.join(data_home, "applications", LINUX_DESKTOP),
             os.path.join(config_home, "autostart", LINUX_AUTOSTART)]
    paths += [os.path.join(d, LINUX_DESKTOP) for d in _linux_desktop_dirs()]
    return sum(1 for p in paths if _remove_if_ours(p))


# ======================================================================

def main():
    print("=== Bodycraft · Uninstall ===")
    print("[1] Stopping background processes")
    stop_by_flag(RUN_FLAG, "Server")
    stop_by_flag(WIDGET_FLAG, "Reminder bar")
    leftovers = find_leftovers()
    if leftovers:
        print("    %d more Bodycraft process(es) still running from this folder; stopping them too" % len(leftovers))
        for pid in leftovers:
            _kill(pid, "Leftover process")

    print("[2] Removing shortcuts and login items")
    if IS_WIN:
        removed = remove_shortcuts_windows()
    elif IS_MAC:
        removed = remove_shortcuts_mac()
    else:
        removed = remove_shortcuts_linux()
    if not removed:
        print("    Nothing to remove")

    print("[3] Your data")
    present = [x for x in DATA_FILES if os.path.exists(os.path.join(BASE, x))]
    if not present:
        print("    No data files here")
    else:
        print("    This folder has the following data:")
        for name in present:
            print("        -", name + ("/" if os.path.isdir(os.path.join(BASE, name)) else ""))
        print()
        print("    It's kept by default. If you keep it and later put a new version in this same")
        print("    folder, it will pick up this data as-is: it won't ask for your goal weight")
        print("    again, and your API key will still be there.")
        print("    To start from zero (and go through the setup wizard again), clear it here.")
        try:
            ans = input("    Clear this data too? (y = clear / Enter = keep): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            ans = ""
        if ans in ("y", "yes"):
            bak = os.path.join(BASE, "uninstall-backup_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
            os.makedirs(bak, exist_ok=True)
            for name in present:
                src = os.path.join(BASE, name)
                try:
                    shutil.move(src, os.path.join(bak, name))
                except Exception as e:
                    print("        Couldn't move", name, "-", e)
            print("    Cleared. Everything was moved into:", os.path.basename(bak))
            print("    Once you're sure you don't need it, delete that backup folder as well.")
        else:
            print("    Kept.")

    print("[4] Checking whether anything still uses this folder")
    still = find_leftovers()
    if still:
        print("    Still running: PID %s" % ", ".join(str(x) for x in still))
    else:
        print("    No Bodycraft processes are using it anymore")
    if IS_WIN:
        print("    If Windows still says the folder is \"in use\" when you delete it, it's usually one of these:")
        print("      - the browser window with Bodycraft open: close it")
        print("      - File Explorer showing this folder: go up one level, then delete")

    print("\n=== Done ===")
    print("Shortcuts and login items have been removed.")
    print("Bodycraft lives entirely in this one folder. It doesn't install system services")
    if IS_WIN:
        print("or write to the registry. Delete the folder and it's gone for good.")
    else:
        print("or anything outside your home folder. Delete the folder and it's gone for good.")
    print()
    print("Tip: if you plan to reinstall and want to go through the setup wizard again, besides")
    print("clearing the data above, also clear this site's data in your browser. The browser keeps")
    print("it by address (127.0.0.1:8765), no matter which folder Bodycraft is in.")


if __name__ == "__main__":
    main()
