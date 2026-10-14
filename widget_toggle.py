# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""Reminder bar on/off switch: run it once and the bar closes if it's running, or starts if it isn't.

Works on Windows, macOS and Linux. It only ever stops the process recorded in the bar's own run
flag (.widget_running.json), and only after checking it really is a Python process.
"""
import os
import sys
import json
import shutil
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
WIDGET = os.path.join(BASE, "bodycraft_widget.py")
RUN_FLAG = os.path.join(BASE, ".widget_running.json")

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
CREATE_NO_WINDOW = 0x08000000


def say(msg):
    try:
        print(msg)
    except Exception:
        pass   # pythonw has no console


def find_python():
    """A Python that has tkinter. On Windows prefer pythonw.exe (no console window)."""
    cands = []
    if IS_WIN:
        d = os.path.dirname(sys.executable)
        cands += [os.path.join(d, "pythonw.exe"), os.path.join(d, "python.exe")]
        names = ("pythonw.exe", "python.exe", "python")
    else:
        names = ("python3", "python")
    cands.append(sys.executable)
    for name in names:
        p = shutil.which(name)
        if p:
            cands.append(p)
    for p in cands:
        if not p or not os.path.exists(p):
            continue
        try:
            kw = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}
            r = subprocess.run([p, "-c", "import tkinter"], capture_output=True, **kw)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return None


def _is_our_process(pid):
    """Is this PID alive, and is it a Python process? (Guards against a reused PID.)"""
    if IS_WIN:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            pass
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                                 capture_output=True, creationflags=CREATE_NO_WINDOW).stdout
            return "python" in out.decode("latin-1", "ignore").lower()
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,command="],
                             capture_output=True, text=True, timeout=5).stdout or ""
        out = out.strip()
        # "Z" = already exited, just not reaped yet
        return bool(out) and not out.startswith("Z") and "python" in out.lower()
    except Exception:
        return False


def running_pid():
    if not os.path.exists(RUN_FLAG):
        return None
    try:
        with open(RUN_FLAG, "r", encoding="utf-8") as f:
            pid = int(json.load(f).get("pid", 0))
    except Exception:
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    return pid if _is_our_process(pid) else None


def _clear_flag():
    try:
        if os.path.exists(RUN_FLAG):
            os.remove(RUN_FLAG)
    except Exception:
        pass


def stop(pid):
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    _clear_flag()
    say("Reminder bar closed.")


def start():
    py = find_python()
    if not py:
        say("Couldn't find a Python with tkinter, which the reminder bar needs.")
        if IS_MAC:
            say("If you use Homebrew Python: brew install python-tk")
        elif not IS_WIN:
            say("On Debian/Ubuntu: sudo apt install python3-tk   (Fedora: sudo dnf install python3-tkinter)")
        else:
            say("Reinstall Python from python.org with \"tcl/tk and IDLE\" checked.")
        return False
    if IS_WIN:
        flags = 0x00000008 | 0x00000200 | 0x01000000
        try:
            subprocess.Popen([py, WIDGET], creationflags=flags, close_fds=True, cwd=BASE)
        except Exception:
            try:
                subprocess.Popen([py, WIDGET], creationflags=0x00000008,
                                 close_fds=True, cwd=BASE)
            except Exception:
                return False
        return True
    try:
        subprocess.Popen([py, WIDGET], cwd=BASE, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception:
        return False
    say("Reminder bar started. Run this again to close it.")
    return True


def main():
    pid = running_pid()
    if pid:
        stop(pid)
    else:
        _clear_flag()     # a stale flag from a bar that's no longer running
        start()


if __name__ == "__main__":
    main()
