# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""Open Bodycraft: make sure the local server is running, then open the app in its own window.

Works on Windows, macOS and Linux.

If port 8765 is taken, the server moves on to the next free port (up to 8776) and writes the
port it actually got into .server_running.json. We read that file instead of assuming 8765.

Options:
  --no-browser               start (or check) the server and print the address, but don't open a window.
  BODYCRAFT_NO_BROWSER=1     same thing, as an environment variable.
"""
import os
import sys
import json
import time
import shutil
import socket
import pathlib
import tempfile
import subprocess
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(BASE, "bodycraft_server.py")
HTML = os.path.join(BASE, "bodycraft.html")
RUN_FLAG = os.path.join(BASE, ".server_running.json")
SERVER_LOG = os.path.join(BASE, ".server.log")
DEFAULT_PORT = 8765
PORT_SPAN = 12          # same range the server uses: 8765..8776

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# Windows process creation flags
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def say(msg):
    """Print, but never crash when there is no console (pythonw on Windows)."""
    try:
        print(msg)
    except Exception:
        pass


# ---------- finding a browser ----------

def _windows_browsers():
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    if local:
        # per-user Chrome installs land here
        cands.append(os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"))
    return cands


def _mac_browsers():
    apps = [
        ("Google Chrome.app", "Google Chrome"),
        ("Microsoft Edge.app", "Microsoft Edge"),
        ("Brave Browser.app", "Brave Browser"),
    ]
    roots = ["/Applications", os.path.expanduser("~/Applications")]
    return [os.path.join(root, app, "Contents", "MacOS", exe)
            for app, exe in apps for root in roots]


LINUX_BROWSERS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                  "microsoft-edge", "microsoft-edge-stable", "brave-browser"]


def find_app_browser():
    """A Chromium-based browser that supports --app= (a clean window with no tabs or address bar)."""
    if IS_WIN:
        cands = _windows_browsers()
    elif IS_MAC:
        cands = _mac_browsers()
    else:
        for name in LINUX_BROWSERS:
            p = shutil.which(name)
            if p:
                return p
        return None
    for p in cands:
        if os.path.exists(p):
            return p
    return None


# ---------- finding Python for the server ----------

def find_server_python():
    """On Windows prefer pythonw.exe, so the server runs without a console window."""
    if IS_WIN:
        d = os.path.dirname(sys.executable)
        pyw = os.path.join(d, "pythonw.exe")
        if os.path.exists(pyw):
            return pyw
        for name in ("pythonw.exe", "python.exe", "python"):
            p = shutil.which(name)
            if p:
                return p
    return sys.executable


# ---------- is the server up? ----------

def port_alive(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.4)
        s.close()
        return True
    except Exception:
        return False


def read_flag():
    """(port, pid) from the server's run flag, or (None, None)."""
    try:
        with open(RUN_FLAG, "r", encoding="utf-8") as f:
            info = json.load(f)
        return int(info.get("port") or DEFAULT_PORT), int(info.get("pid") or 0) or None
    except Exception:
        return None, None


def version_info(port):
    """What the server on this port says about itself via /api/version, or None."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/version" % port, timeout=1.5) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if isinstance(d, dict) and d.get("build") else None
    except Exception:
        return None


def running_build(port):
    """The build id the running server reports, or None if it doesn't answer."""
    return (version_info(port) or {}).get("build")


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def runs_our_server(pid):
    """Is this PID a server started from *this* folder?

    Another copy of Bodycraft in a different folder answers /api/version exactly the same way,
    but it has different data. Opening that one would look like your records had vanished.
    """
    if not pid:
        return False
    try:
        if IS_WIN:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % int(pid)],
                capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            return SERVER.lower() in (r.stdout or "").lower()
        r = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                           capture_output=True, text=True, timeout=5)
        cmd = r.stdout or ""
        if SERVER in cmd:
            return True
        if "bodycraft_server.py" not in cmd:
            return False
        # started with a relative path: check the process's working directory
        cwd = None
        if os.path.isdir("/proc/%d" % pid):
            cwd = os.readlink("/proc/%d/cwd" % pid)
        elif shutil.which("lsof"):
            out = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"],
                                 capture_output=True, text=True, timeout=5).stdout or ""
            for line in out.splitlines():
                if line.startswith("n"):
                    cwd = line[1:]
        return bool(cwd) and _same_path(cwd, BASE)
    except Exception:
        return False


def running_port():
    """Which port this folder's server is listening on, or None.

    The run flag comes first: the server writes its PID and port there right after it starts,
    and we confirm the server on that port reports the same PID. Without a usable flag, scan
    8765..8776, but only accept a server that was started from this folder. Another program
    on 8765, or another Bodycraft copy, must not be mistaken for ours.
    """
    port, pid = read_flag()
    if port and port_alive(port):
        info = version_info(port)
        # No answer yet is fine: the server writes the flag before it finishes warming up.
        # A different PID means the flag is stale and someone else has that port now.
        if not info or not pid or not info.get("pid") or int(info.get("pid")) == pid:
            return port
    for p in range(DEFAULT_PORT, DEFAULT_PORT + PORT_SPAN):
        if not port_alive(p):
            continue
        info = version_info(p)
        if info and runs_our_server(info.get("pid")):
            return p
    return None


def file_build():
    """The build id of the server file on disk (the BUILD = "..." line)."""
    try:
        with open(SERVER, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("BUILD ="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


# ---------- starting the server ----------

def start_server():
    py = find_server_python()
    if IS_WIN:
        # pythonw has no console: send its output to .server.log so a failed start leaves a trace
        try:
            log = open(SERVER_LOG, "ab")
        except Exception:
            log = subprocess.DEVNULL
        io_args = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
            subprocess.Popen([py, SERVER], creationflags=flags, close_fds=True, cwd=BASE, **io_args)
        except Exception:
            # some job objects don't allow breakaway; try again without it
            try:
                subprocess.Popen([py, SERVER], creationflags=DETACHED_PROCESS,
                                 close_fds=True, cwd=BASE, **io_args)
            except Exception:
                return False
        finally:
            if log is not subprocess.DEVNULL:
                log.close()
        return True

    # macOS / Linux: run detached in its own session so closing this terminal doesn't stop it.
    # Output goes to .server.log in this folder, which helps if something goes wrong.
    try:
        log = open(SERVER_LOG, "ab")
    except Exception:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen([py, SERVER], cwd=BASE, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
        return True
    except Exception:
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()     # the child keeps its own copy of the handle


def ensure_server():
    port = running_port()
    if port:
        # Upgrades: after new files are copied over the old ones, the server running in the
        # background is still the old version (it never re-reads its own code). If the build ids
        # differ, start a new one. On startup the server stops the old instance using the PID in
        # its run flag, so "copy the new files, double-click the icon" is all anyone has to do.
        old = running_build(port)
        new = file_build()
        if not (old and new and old != new):
            return port
        say("Updating the background server from %s to %s..." % (old, new))
        if not start_server():
            return port
        for _ in range(60):
            time.sleep(0.1)
            p2 = running_port()
            if p2 and running_build(p2) == new:
                return p2
        return running_port() or port
    if not start_server():
        return None
    # a cold start on Windows (antivirus scanning a fresh Python process) can take several seconds
    for _ in range(150):
        time.sleep(0.1)
        port = running_port()
        if port:
            return port
    return None


# ---------- opening the window ----------

def open_url(url):
    """Open the app window.

    About cwd: if we don't set it, the browser inherits this folder as its working directory.
    On Windows that keeps the folder locked for as long as the browser stays open ("the folder
    is in use by another program"). The browser doesn't need this folder at all.
    """
    outside = tempfile.gettempdir()
    browser = find_app_browser()
    if browser:
        try:
            if IS_WIN:
                subprocess.Popen([browser, "--app=" + url], creationflags=CREATE_NO_WINDOW,
                                 close_fds=True, cwd=outside)
            else:
                subprocess.Popen([browser, "--app=" + url], cwd=outside,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 close_fds=True)
            return
        except Exception:
            pass
    if IS_MAC:
        try:
            subprocess.Popen(["open", url], cwd=outside)
            return
        except Exception:
            pass
    import webbrowser
    webbrowser.open(url)


def browser_disabled():
    if "--no-browser" in sys.argv[1:]:
        return True
    return os.environ.get("BODYCRAFT_NO_BROWSER", "").strip().lower() in ("1", "true", "yes")


def main():
    port = ensure_server()
    if port:
        url = "http://127.0.0.1:%d/" % port
        if browser_disabled():
            say("Bodycraft is running at %s" % url)
            return 0
        open_url(url)
        return 0

    # The server couldn't start. Fall back to the file:// version: data is kept in the
    # browser's local storage. Fewer features, but it still works.
    say("Couldn't start the Bodycraft server.")
    say("Details may be in %s" % SERVER_LOG)
    if browser_disabled():
        return 1
    say("Opening the offline version instead (your entries stay in this browser only).")
    import webbrowser
    webbrowser.open(pathlib.Path(HTML).as_uri())
    return 1


if __name__ == "__main__":
    sys.exit(main())
