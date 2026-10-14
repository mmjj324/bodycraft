# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""Bodycraft · desktop reminder bar (small card, tucks away at the screen edge).

A clean white card with a colored accent: compact, stays out of your way. Drag it to the left or
right edge of the screen and it snaps there and tucks itself away, leaving just a thin colored strip.
Hover to slide it out; move away and it tucks back in.
Double-click to open Bodycraft, right-click to quit. It pops up a gentle reminder at key times.

Runs on Windows, macOS and Linux (needs tkinter). It reads your routine straight from content.json
in this folder, the same file the Bodycraft server writes when you edit your routine in the app,
and picks up changes on its own.
"""
import os
import sys
import json
import datetime
import subprocess
import tkinter as tk

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, "bodycraft.html")
OPENER = os.path.join(BASE, "start.py")
POS_FILE = os.path.join(BASE, ".widget_pos.json")
RUN_FLAG = os.path.join(BASE, ".widget_running.json")

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# The app's default workday (same as `let schedule = [` in bodycraft.html).
# Used until content.json has a routine of its own.
DEFAULT_SCHEDULE = [
    {"t": "06:30", "e": "06:45", "title": "Wake up", "type": "life", "detail": "A glass of water first thing"},
    {"t": "06:45", "e": "07:30", "title": "Morning walk", "type": "exercise", "detail": "Brisk walk, about 30 minutes"},
    {"t": "07:30", "e": "07:50", "title": "Breakfast", "type": "meal", "detail": "Protein-forward breakfast"},
    {"t": "07:50", "e": "08:30", "title": "Commute / school drop-off", "type": "family", "detail": "Out the door"},
    {"t": "08:30", "e": "12:00", "title": "Morning work", "type": "work", "detail": "Sip 2 cups of water"},
    {"t": "12:00", "e": "13:00", "title": "Lunch", "type": "meal", "detail": "Balanced lunch"},
    {"t": "13:00", "e": "15:30", "title": "Afternoon work", "type": "work", "detail": "Sip 2 more cups of water"},
    {"t": "15:30", "e": "15:45", "title": "Walk and stretch break", "type": "exercise", "detail": "Short walk or a few stretches"},
    {"t": "15:45", "e": "17:30", "title": "Finish the workday", "type": "work", "detail": ""},
    {"t": "17:30", "e": "18:30", "title": "Commute and family time", "type": "family", "detail": ""},
    {"t": "18:30", "e": "19:15", "title": "Family dinner", "type": "meal", "detail": "Protein and veggies"},
    {"t": "19:15", "e": "19:45", "title": "After-dinner walk", "type": "exercise", "detail": "15-30 minute stroll"},
    {"t": "19:45", "e": "22:00", "title": "Free evening", "type": "life", "detail": ""},
    {"t": "22:00", "e": "22:30", "title": "Wind down", "type": "rest", "detail": "Screens off, lights low"},
]

SCHEDULE = [(s["t"], s["e"], s["title"], s["type"]) for s in DEFAULT_SCHEDULE]

# Category colors and names. Same muted palette as the app;
# "type_meta" in content.json overrides these, so custom categories show up here too.
COLORS = {
    "life": "#1D6B4F", "family": "#665080", "exercise": "#A9552C",
    "meal": "#9C6B22", "work": "#3D5C8C", "rest": "#3E7F76",
}
COLOR_NAMES = {"life": "Life", "family": "Family", "exercise": "Exercise",
               "meal": "Meals", "work": "Work", "rest": "Rest"}

CARD_BG = "#FFFFFF"
SHADOW = "#E3E7EC"
INK = "#1F2937"
INK_SUB = "#6B7280"
INK_FAINT = "#C0C7CF"
TRANSPARENT = "#010203"

# Fonts: Segoe UI on Windows, SF Pro / Helvetica Neue on macOS, DejaVu Sans on Linux.
# The first family that's installed wins; if none is, Tk quietly uses its default font.
if IS_WIN:
    FONT_CANDIDATES = ["Segoe UI"]
elif IS_MAC:
    FONT_CANDIDATES = ["SF Pro Text", ".AppleSystemUIFont", "Helvetica Neue", "Helvetica"]
else:
    FONT_CANDIDATES = ["DejaVu Sans", "Liberation Sans", "Noto Sans"]
FONT = FONT_CANDIDATES[0]


def _pt(n):
    """Tk on macOS draws 1 point = 1 pixel, so text looks about a quarter smaller than on
    Windows/Linux. Scale it up there so the card reads the same everywhere."""
    return int(round(n * 4 / 3)) if IS_MAC else n


def _fonts():
    return {
        "time": (FONT, _pt(18), "bold"),
        "ampm": (FONT, _pt(9), "bold"),
        "act": (FONT, _pt(11), "bold"),
        "sub": (FONT, _pt(8)),
        "menu": (FONT, _pt(10)),
        "toast_head": (FONT, _pt(9), "bold"),
        "toast": (FONT, _pt(11)),
    }


EDGE_MARGIN = 40   # snap distance: closer than this to an edge and it docks there
PEEK = 34          # how much stays visible when tucked away

# Weekend behavior:
#   "schedule" (default)  weekends follow the same routine, so edits to your routine apply there too
#   "free"                weekends show "Weekend" on the card, but reminders still fire
# An older version hard-coded the weekend as "free all day / family / swim / errands". It ignored
# content.json and silenced every reminder all weekend, the bedtime one included, right after
# the user had the AI tune their weekend routine.
WEEKEND_MODE = "schedule"
WEEKEND_LABEL = ("Weekend", "Free all day", "Family time, errands, a swim")


def to_min(hhmm, default=None):
    """Turn HH:MM into minutes after midnight. A malformed string gives `default` instead of an error.

    Times in content.json come from an AI model and can be empty or something like "morning".
    Splitting those blindly would raise inside update() and freeze the card at that moment.
    """
    try:
        h, m = str(hhmm).split(":")
        v = int(h) * 60 + int(m)
        if 0 <= v < 1440:
            return v
    except Exception:
        pass
    return default


def reminders_from(schedule):
    """Reminders that follow the routine: one at the start of each block, except work blocks."""
    rem = []
    for s in schedule:
        if not isinstance(s, dict) or s.get("type") == "work":
            continue
        v = to_min(str(s.get("t", "")))
        if v is None:
            continue
        msg = str(s.get("title", "")).strip()
        if str(s.get("detail", "")).strip():
            msg += ": " + str(s["detail"]).strip()
        if msg:
            rem.append((v, msg))
    return rem


REMINDERS = reminders_from(DEFAULT_SCHEDULE)


def clock_12h(now):
    """("7:05", "AM") style, without relying on platform-specific strftime flags."""
    h = now.hour % 12 or 12
    return "%d:%02d" % (h, now.minute), ("AM" if now.hour < 12 else "PM")


def span_text(minutes):
    hh, mm = divmod(int(minutes), 60)
    return "%dh %02dm" % (hh, mm) if hh > 0 else "%d min" % mm


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def schedule_weekday(now=None):
    """Day of the week for routine purposes (0 = Sunday, same as the app).
    Before 4 a.m. still counts as the previous day: staying up until 1 on Friday isn't the weekend yet."""
    now = now or datetime.datetime.now()
    if now.hour < 4:
        now = now - datetime.timedelta(days=1)
    return (now.weekday() + 1) % 7


def pick_schedule(c, now=None):
    """Which routine applies today: the weekend one, if you've set one and today is one of your weekend days."""
    wk = c.get("schedule_weekend")
    days = c.get("weekend_days")
    if not isinstance(days, list):
        days = [6, 0]
    if isinstance(wk, list) and wk and schedule_weekday(now) in [d for d in days if isinstance(d, int)]:
        return wk
    return c.get("schedule")


def load_content_override():
    """Take the routine and reminders from content.json, if it exists."""
    global SCHEDULE, REMINDERS, WEEKEND_MODE, COLORS, COLOR_NAMES
    try:
        with open(os.path.join(BASE, "content.json"), "r", encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        return
    if not isinstance(c, dict):
        return
    # Custom categories: colors and names follow the app. Otherwise a newly added
    # category wouldn't show up here, or would show in the wrong color.
    tm = c.get("type_meta")
    if isinstance(tm, dict) and tm:
        for k, v in tm.items():
            if isinstance(v, dict):
                if v.get("c"):
                    COLORS[k] = v["c"]
                if v.get("name"):
                    COLOR_NAMES[k] = v["name"]
    today_sched = pick_schedule(c)
    if isinstance(today_sched, list) and today_sched:
        sched = []
        for s in today_sched:
            if isinstance(s, dict):
                item = (str(s.get("t", "")), str(s.get("e", "")),
                        str(s.get("title", "")), str(s.get("type", "life")))
            elif isinstance(s, (list, tuple)) and len(s) >= 4:
                item = tuple(str(x) for x in s[:4])
            else:
                continue
            # drop entries with a bad time so they can't break update()
            if to_min(item[0]) is None or to_min(item[1]) is None:
                continue
            if item[3] not in COLORS:
                item = (item[0], item[1], item[2], "life")
            sched.append(item)
        if sched:
            # Don't sort by clock time: a routine already runs from waking up to going to sleep.
            # Someone on night shift gets up at 4 p.m. and sleeps at 8 a.m.; sorted by the clock,
            # the first block would be "work" at 2 a.m. and the "before wake-up = asleep" logic breaks.
            SCHEDULE = sched
            # With no separate reminder list, reminders follow the routine. They used to be
            # hard-coded to a 7:00 wake-up and a midnight "go to bed, don't stay up late". After
            # switching to a night-shift routine, that popped up while the person was driving to work.
            if not (isinstance(c.get("reminders"), list) and c["reminders"]):
                rem = reminders_from(today_sched)
                if rem:
                    REMINDERS = rem
    if isinstance(c.get("reminders"), list) and c["reminders"]:
        rem = []
        for r in c["reminders"]:
            if isinstance(r, (list, tuple)) and len(r) >= 2:
                v = to_min(str(r[0]))
                if v is not None:
                    rem.append((v, str(r[1])))
        if rem:
            REMINDERS = rem
    prof = c.get("profile")
    if isinstance(prof, dict) and prof.get("weekend_mode") in ("schedule", "free"):
        WEEKEND_MODE = prof["weekend_mode"]


def find_python():
    """The Python to run start.py with. On Windows prefer pythonw.exe (no console window)."""
    if IS_WIN:
        import shutil
        d = os.path.dirname(sys.executable)
        pyw = os.path.join(d, "pythonw.exe")
        if os.path.exists(pyw):
            return pyw
        for name in ("pythonw.exe", "python.exe", "python"):
            p = shutil.which(name)
            if p:
                return p
    return sys.executable


class Widget:
    def __init__(self):
        global FONT
        self.root = tk.Tk()
        self.root.title("Bodycraft")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        FONT = self._pick_font()
        self.F = _fonts()
        self._bg = self._setup_transparency(self.root)

        self.w, self.h = 228, 88
        x, y, dock = self.load_pos()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if x is None:
            x = sw - self.w - 20
            y = sh - self.h - 84
        x = max(0, min(x, sw - self.w))
        y = max(0, min(y, sh - self.h))
        self._docked = dock if dock in ("left", "right") else None
        self._hidden = False
        self._collapse_job = None

        self.root.geometry("%dx%d+%d+%d" % (self.w, self.h, x, y))

        self.cv = tk.Canvas(self.root, width=self.w, height=self.h,
                            highlightthickness=0, bg=self._bg)
        self.cv.pack()

        self._time = "--:--"
        self._ampm = ""
        self._act = "Loading..."
        self._tag_color = "life"
        self._count = ""
        self._next = ""
        self._day = ""
        self._reminded = set()

        self.cv.bind("<Button-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.cv.bind("<Double-Button-1>", self.open_html)
        # Right-click is Button-3 on Windows/Linux; on macOS Tk 8.6 reports it as Button-2.
        self.cv.bind("<Button-3>", self.show_menu)
        if IS_MAC:
            self.cv.bind("<Button-2>", self.show_menu)
        self.cv.bind("<Enter>", self.on_enter)
        self.cv.bind("<Leave>", self.on_leave)

        self.menu = tk.Menu(self.root, tearoff=0, bg=CARD_BG, fg=INK,
                            activebackground="#3B82F6", activeforeground="#fff",
                            font=self.F["menu"])
        self.menu.add_command(label="Open Bodycraft", command=self.open_html)
        self.menu.add_separator()
        self.menu.add_command(label="Quit reminder bar", command=self.quit)

        try:
            with open(RUN_FLAG, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(),
                           "start": datetime.datetime.now().isoformat()}, f)
        except Exception:
            pass

        # widget_toggle.py stops the bar with SIGTERM on macOS/Linux: save the position and clean up
        if not IS_WIN:
            try:
                import signal
                signal.signal(signal.SIGTERM, lambda *a: self.root.after(0, self.quit))
            except Exception:
                pass

        if self._docked:
            self.collapse()
        self.update()

    # ---------- platform bits ----------
    def _pick_font(self):
        try:
            import tkinter.font as tkfont
            installed = set(tkfont.families(self.root))
            for fam in FONT_CANDIDATES:
                if fam in installed:
                    return fam
        except Exception:
            pass
        return FONT_CANDIDATES[0]

    @staticmethod
    def _setup_transparency(win):
        """Rounded corners need a see-through window: a color key on Windows, a transparent
        window on macOS. Most Linux window managers have neither, so there the card simply
        sits on a white background."""
        if IS_WIN:
            try:
                win.attributes("-transparentcolor", TRANSPARENT)
                return TRANSPARENT
            except Exception:
                return CARD_BG
        if IS_MAC:
            try:
                win.attributes("-transparent", True)
                win.config(bg="systemTransparent")
                return "systemTransparent"
            except Exception:
                return CARD_BG
        return CARD_BG

    def _fit(self, text, font, max_w):
        """Shorten text with an ellipsis so it fits in max_w pixels."""
        try:
            cache = self.__dict__.setdefault("_font_cache", {})
            f = cache.get(font)
            if f is None:
                import tkinter.font as tkfont
                f = cache[font] = tkfont.Font(root=self.root, font=font)
            if f.measure(text) <= max_w:
                return text
            while text and f.measure(text + "...") > max_w:
                text = text[:-1]
            return text.rstrip() + "..."
        except Exception:
            return text

    # ---------- position & docking ----------
    def load_pos(self):
        try:
            with open(POS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("x"), d.get("y"), d.get("dock")
        except Exception:
            return None, None, None

    def save_pos(self):
        try:
            d = {"y": self.root.winfo_y(), "dock": self._docked}
            sw = self.root.winfo_screenwidth()
            if self._docked == "left":
                d["x"] = 0
            elif self._docked == "right":
                d["x"] = sw - self.w
            else:
                d["x"] = self.root.winfo_x()
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception:
            pass

    def expand(self):
        if not self._docked:
            return
        sw = self.root.winfo_screenwidth()
        y = self.root.winfo_y()
        if self._docked == "left":
            self.root.geometry("+%d+%d" % (0, y))
        else:
            self.root.geometry("+%d+%d" % (sw - self.w, y))
        self._hidden = False
        self.draw()

    def collapse(self):
        if not self._docked:
            return
        sw = self.root.winfo_screenwidth()
        y = self.root.winfo_y()
        if self._docked == "left":
            self.root.geometry("+%d+%d" % (-(self.w - PEEK), y))
        else:
            self.root.geometry("+%d+%d" % (sw - PEEK, y))
        self._hidden = True
        self.draw()

    def snap_and_dock(self):
        sw = self.root.winfo_screenwidth()
        x = self.root.winfo_x()
        if x < EDGE_MARGIN:
            self._docked = "left"
            self.collapse()
        elif x + self.w > sw - EDGE_MARGIN:
            self._docked = "right"
            self.collapse()
        else:
            self._docked = None
            self._hidden = False

    def on_press(self, e):
        if self._hidden and self._docked:
            self.expand()
        self._docked = None
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def on_drag(self, e):
        self.root.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    def on_release(self, e):
        self.snap_and_dock()
        self.save_pos()

    def on_enter(self, e):
        if self._collapse_job:
            self.root.after_cancel(self._collapse_job)
            self._collapse_job = None
        if self._docked and self._hidden:
            self.expand()

    def on_leave(self, e):
        if self._docked and not self._hidden:
            self._collapse_job = self.root.after(400, self.collapse)

    def show_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def open_html(self, e=None):
        """Open the full app through start.py, which starts the server first if needed."""
        try:
            if IS_WIN:
                subprocess.Popen([find_python(), OPENER], creationflags=0x08000000, cwd=BASE)
            else:
                subprocess.Popen([find_python(), OPENER], cwd=BASE,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except Exception:
            pass
        # Last resort: open the page file directly (offline mode)
        try:
            if IS_WIN:
                os.startfile(HTML)
            elif IS_MAC:
                subprocess.Popen(["open", HTML])
            else:
                import webbrowser
                import pathlib
                webbrowser.open(pathlib.Path(HTML).as_uri())
        except Exception:
            pass

    def quit(self):
        self.save_pos()
        try:
            if os.path.exists(RUN_FLAG):
                os.remove(RUN_FLAG)
        except Exception:
            pass
        self.root.destroy()

    # ---------- drawing ----------
    def draw(self):
        cv = self.cv
        cv.delete("all")
        w, h = self.w, self.h
        F = self.F
        color = COLORS.get(self._tag_color, "#3B82F6")

        round_rect(cv, 2, 4, w - 2, h - 2, 14, fill=SHADOW, outline="")
        round_rect(cv, 0, 0, w - 3, h - 5, 14, fill=CARD_BG, outline="")

        if self._hidden:
            # tucked away: only the colored strip shows
            if self._docked == "left":
                round_rect(cv, w - 14, 12, w - 8, h - 14, 3, fill=color, outline="")
            else:
                round_rect(cv, 8, 12, 14, h - 14, 3, fill=color, outline="")
            return

        # colored strip on the left
        round_rect(cv, 8, 12, 14, h - 12, 3, fill=color, outline="")

        t = cv.create_text(22, 28, text=self._time, font=F["time"], fill=INK, anchor="w")
        if self._ampm:
            box = cv.bbox(t)
            ax = (box[2] + 3) if box else 90
            cv.create_text(ax, 33, text=self._ampm, font=F["ampm"], fill=INK_SUB, anchor="w")
        if self._count:
            cv.create_text(w - 13, 28, text=self._count, font=F["sub"], fill=INK_SUB, anchor="e")

        cv.create_oval(22, 44, 28, 50, fill=color, outline="")
        cv.create_text(33, 47, text=self._fit(self._act, F["act"], w - 33 - 12),
                       font=F["act"], fill=INK, anchor="w")

        cv.create_text(22, 67, text=self._fit(self._next, F["sub"], w - 22 - 12),
                       font=F["sub"], fill=INK_SUB, anchor="w")

    def show_toast(self, msg):
        try:
            F = self.F
            toast = tk.Toplevel(self.root)
            toast.overrideredirect(True)
            toast.attributes("-topmost", True)
            try:
                toast.attributes("-alpha", 0.0)
            except Exception:
                pass
            bg = self._setup_transparency(toast)
            tw, th = 320, 84
            tc = tk.Canvas(toast, width=tw, height=th, highlightthickness=0, bg=bg)
            tc.pack()
            round_rect(tc, 2, 4, tw - 2, th - 2, 14, fill=SHADOW, outline="")
            round_rect(tc, 0, 0, tw - 3, th - 5, 14, fill=CARD_BG, outline="")
            round_rect(tc, 8, 13, 14, th - 15, 3, fill="#F59E0B", outline="")
            tc.create_text(24, 20, text="Reminder", font=F["toast_head"],
                           fill="#B45309", anchor="w")
            tc.create_text(24, 44, text=msg, font=F["toast"],
                           fill=INK, anchor="w", width=tw - 38)

            x = self.root.winfo_x() + self.w - tw
            y = self.root.winfo_y() - th - 10
            x = max(0, min(x, self.root.winfo_screenwidth() - tw))
            y = max(0, y)
            toast.geometry("%dx%d+%d+%d" % (tw, th, x, y))

            def fade_in(a=0.0):
                if a < 1.0:
                    a = min(1.0, a + 0.09)
                    try:
                        toast.attributes("-alpha", a)
                    except Exception:
                        return
                    toast.after(18, lambda: fade_in(a))
            fade_in()
            toast.after(6000, toast.destroy)
            tc.bind("<Button-1>", lambda e: toast.destroy())
        except Exception:
            pass

    def update(self):
        now = datetime.datetime.now()
        self._time, self._ampm = clock_12h(now)
        m = now.hour * 60 + now.minute
        day = now.strftime("%Y%m%d")
        if day != self._day:
            self._day = day
            self._reminded = set()
        # Pick up routine edits made in the app, or a one-click switch to another shift
        # (this used to be read only once, at startup).
        try:
            mt = os.path.getmtime(os.path.join(BASE, "content.json"))
        except OSError:
            mt = None
        # Friday night after 4 a.m. switches to the weekend routine; Sunday night after 4 a.m. switches back
        sday = schedule_weekday(now)
        if mt != getattr(self, "_content_mtime", None) or sday != getattr(self, "_sched_day", None):
            self._content_mtime = mt
            self._sched_day = sday
            load_content_override()
        is_weekend = now.weekday() >= 5

        if not SCHEDULE:
            self._tag_color, self._act = "life", "No routine set"
            self._count, self._next = "", "Set one up in Bodycraft"
            self.draw()
            self.root.after(30000, self.update)
            return

        if is_weekend and WEEKEND_MODE == "free":
            self._tag_color, self._act = "family", WEEKEND_LABEL[0]
            self._count, self._next = WEEKEND_LABEL[1], WEEKEND_LABEL[2]
        else:
            # current block; watch out for blocks that cross midnight (e.g. 23:30-00:00)
            cur = None
            for s in SCHEDULE:
                a, b = to_min(s[0]), to_min(s[1])
                if a is None or b is None:
                    continue
                if b <= a:            # crosses midnight
                    if m >= a or m < b:
                        cur = s
                        break
                elif a <= m < b:
                    cur = s
                    break

            first, last = SCHEDULE[0], SCHEDULE[-1]
            wake = to_min(first[0])
            sleep = to_min(last[1])
            if cur is None:
                # A gap the routine doesn't cover. An older version hard-coded SCHEDULE[12] /
                # SCHEDULE[11] here: change the number of blocks and it raised IndexError, freezing
                # the card. Now "between bedtime and wake-up = asleep", with no index involved.
                # Measured around the clock, so a night-shift routine that sleeps by day works too.
                in_sleep = (wake is not None and sleep is not None
                            and (m - sleep) % 1440 < (wake - sleep) % 1440)
                if in_sleep:
                    left = (wake - m) % 1440
                    self._tag_color, self._act = "rest", "Sleep time"
                    self._count = "wake in " + span_text(left)
                    self._next = "Next: %s" % first[2]
                    self.draw()
                    self.root.after(30000, self.update)
                    return
                # Awake but outside every block: free time. (This used to show the last block,
                # "Wind down", telling people to get ready for bed at 5 in the afternoon.)
                cur = ("", "", "Free time", "life")
            self._tag_color = cur[3] if cur[3] in COLORS else "life"
            self._act = cur[2]

            # next block: the nearest upcoming start time (wrapping past midnight counts)
            nxt, best = first, None
            for s in SCHEDULE:
                a = to_min(s[0])
                if a is None:
                    continue
                d = (a - m) % 1440
                if d > 0 and (best is None or d < best):
                    nxt, best = s, d
            na = to_min(nxt[0])
            remain = (na - m) if na is not None else 0
            if remain < 0:
                remain += 1440
            self._count = "in " + span_text(remain)
            self._next = "Next: %s" % nxt[2]

        # Reminders live outside the weekday branch. An older version went silent all weekend,
        # bedtime reminder included, and that's exactly the one weekends need most.
        for rm, msg in REMINDERS:
            if m == rm and rm not in self._reminded:
                self._reminded.add(rm)
                self.show_toast(msg)

        self.draw()
        self.root.after(1000, self.update)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    load_content_override()
    Widget().run()
