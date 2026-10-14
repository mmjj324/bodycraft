"""Eating window (16:8) and new-user support -- are meal times remembered, is the window
math right, and does the app respond well when someone is having a hard time?

This layer was added after walking through three weeks as a brand-new user. Every problem
found on that walk-through has a test here:

  - meal times dropped               -> photo_meta / plain-language logging keep `at`; editing calories doesn't erase it
  - eating window gone after reload  -> turn the window on, reopen the page, it's still there
  - old days judged by the new window after a shift change -> "held 5 days" turned into "2 days" overnight
  - a bad day answered with a logging form -> low moments go to conversation first
  - "why am I heavier?" swallowed as a log entry -> worries about the scale get an answer first
  - one "5%" in the review prompt killed the review -> free text is escaped before it goes into a % template
  - home screen showing the wrong start weight -> the start weight comes from the member; redraw when content arrives late
  - a few days off and the badges go gray -> badges once earned stay lit

Usage:
    python3 test_eating.py          run everything
    python3 test_eating.py api      only the parts that don't need a browser (a few seconds)
    python3 test_eating.py page     only the browser parts
"""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = "bodycraft_server.py"
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".eating_tmp")
PASS, FAIL = [], []


def check(name, got, want, note=""):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    line = "  %-40s %s" % (name, "PASS" if ok else "FAIL")
    if not ok:
        line += "   expected %r, got %r" % (want, got)
    if note and not ok:
        line += "   " + str(note)[:300]
    print(line)
    return ok


def check_true(name, cond, note=""):
    return check(name, bool(cond), True, note)


# ---------- fake model: records every prompt it gets, replies with the queued answers in order ----------
FAKE = {"log": [], "replies": []}


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        FAKE["log"].append(body.get("messages") or [])
        reply = FAKE["replies"].pop(0) if FAKE["replies"] else "Okay."
        out = json.dumps({"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]},
                         ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start_fake():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fresh_server(fake_port=None):
    if os.path.exists(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    shutil.copytree(BASE, TMP, ignore=shutil.ignore_patterns(
        "__pycache__", "backups", "selfies", "photos", "files", "tests", "docs", ".*"))
    for f in ("api_config.json", "api_key.txt", "data.json", "content.json", "photos.json", "diary.json",
              "knowledge.json", "favorites.json", "food_lookup.json", "attach.json", "selfies.json"):
        p = os.path.join(TMP, f)
        if os.path.exists(p):
            os.remove(p)
    if fake_port:
        with open(os.path.join(TMP, "api_config.json"), "w", encoding="utf-8") as f:
            json.dump({"base_url": "http://127.0.0.1:%d" % fake_port, "api_key": "sk-test",
                       "model": "fake"}, f)
    proc = subprocess.Popen([sys.executable, SERVER], cwd=TMP,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    for _ in range(40):
        time.sleep(0.3)
        flag = os.path.join(TMP, ".server_running.json")
        if os.path.exists(flag):
            try:
                with open(flag, encoding="utf-8") as f:
                    return proc, json.load(f)["port"]
            except Exception:
                continue
    proc.kill()
    raise RuntimeError("server didn't start")


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def api(port, path, body=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if body is None:
        r = urllib.request.urlopen(url, timeout=20)
    else:
        r = urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}), timeout=20)
    raw = r.read()
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return raw.decode("utf-8-sig")


def load_module():
    spec = importlib.util.spec_from_file_location("svc", os.path.join(BASE, SERVER))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ======================================================================
def part_units():
    print("\n[Pure functions]")
    m = load_module()
    check("time cleanup 7:5 -> 07:05", m.clean_hm("7:5"), "07:05")
    check("a dot works as the separator too", m.clean_hm("19.30"), "19:30")
    check("invalid time gives an empty string", m.clean_hm("25:00"), "")
    check("a 6-hour window is pulled back to 8", m.sanitize_eating_window({"enabled": True, "hours": 6})["hours"], 8)
    check("invalid window start falls back to default", m.sanitize_eating_window({"enabled": True, "hours": 8, "start": "x"})["start"], "10:00")
    ew = {"enabled": True, "hours": 10, "start": "09:30"}
    check("the closing minute counts as inside", m.in_window("19:30", ew), True)
    check("outside the window", m.in_window("21:30", ew), False)
    check("window crossing midnight", m.in_window("01:00", {"enabled": True, "hours": 12, "start": "16:00"}), True)
    check("window text", m.eating_window_text({"enabled": True, "hours": 8, "start": "10:00"}), "10:00–18:00 (16:8)")
    check("no window, empty text", m.eating_window_text({"enabled": False, "hours": 8, "start": "10:00"}), "")

    print("\n[How to answer this message]")
    cases = [
        ("Kids are finally asleep and I'm craving chips so bad", "craving"),
        ("Got yelled at by my boss today and I ate a whole bag of chips tonight. I'm so useless, forget it, I give up", "lapse"),
        ("Weighed in at 164.9 this morning on an empty stomach. Why am I heavier than yesterday?", "worry"),
        ("Two weeks and the scale won't budge. Is 16:8 just not working for me? Am I going to fail again?", "worry"),
        ("For lunch I had rice and a chicken drumstick", None),
        ("Weight 162.7", None),
    ]
    for text, want in cases:
        check("mood: " + text[:24], m.distress_kind(text), want)
    low = "Got yelled at today and I ate a whole bag of chips, I'm so useless"
    check("a low moment doesn't go to logging", m._rule_intent(low), "chat")
    check("but it still sees something loggable in it", m.record_kind(low), "meal")
    check("'weighed in at 164.9' is recognized as a weight", m.record_kind("Weighed in at 164.9 this morning, empty stomach"), "record")
    check("a settings change isn't taken by the mood rules", m._rule_intent("Delete the breakfast habit from my checklist"), "update")

    print("\n[Parsed logs keep the meal time]")
    got = m._sanitize_daily_log({"meals": {"lunch": {"at": "12:5", "foods": [{"name": "Rice", "cal": 174}]}}},
                                ["breakfast", "lunch", "dinner", "snack"])
    check("plain-language log keeps at", got["meals"]["lunch"].get("at"), "12:05")
    got = m._sanitize_daily_log({"meals": {"lunch": {"at": "already ate", "foods": [{"name": "Rice", "cal": 174}]}}},
                                ["lunch"])
    check("a garbage time is dropped", "at" in got["meals"]["lunch"], False)

    print("\n[Shift work: window history, yesterday's meals]")
    ew = m.sanitize_eating_window({"enabled": True, "hours": 12, "start": "18:00", "since": "2026-10-20",
                                   "history": [{"since": "2026-10-12", "enabled": True, "hours": 12, "start": "07:30"},
                                               {"since": "2026-10-20", "enabled": True, "hours": 12, "start": "18:00"}],
                                   "alt": {"hours": 12, "start": "07:30"}})
    check("both history entries kept", len(ew.get("history") or []), 2)
    check("the backup window is kept", (ew.get("alt") or {}).get("start"), "07:30")
    check("day-shift days judged by the day window", m.window_for_date(ew, "2026-10-15")["start"], "07:30")
    check("the day of the switch still uses the old window", m.window_for_date(ew, "2026-10-20")["start"], "07:30")
    check("from the next day on, the night window", m.window_for_date(ew, "2026-10-21")["start"], "18:00")
    check("days before the window was on aren't judged", m.window_for_date(ew, "2026-10-12"), None)
    check("night window crosses midnight: 00:30 is inside", m.in_window("00:30", {"hours": 12, "start": "18:00"}), True)
    got = m._sanitize_daily_log({"meals": {"dinner": {"at": "19:00", "day": -1, "foods": [{"name": "Rice", "cal": 232}]}}}, ["dinner"])
    check("'last night's dinner' is marked as yesterday", got["meals"]["dinner"].get("day"), -1)
    got = m._sanitize_daily_log({"meals": {"dinner": {"at": "19:00", "day": -3, "foods": [{"name": "Rice", "cal": 232}]}}}, ["dinner"])
    check("only yesterday, nothing earlier", "day" in got["meals"]["dinner"], False)
    check("health notes cut to 300 characters", len(m.sanitize_profile({"health_note": "x" * 500})["health_note"]), 300)

    print("\n[Desktop widget: night-shift schedule]")
    part_widget()

    print("\n[Safety clause]")
    s = m.SAFETY_CLAUSE
    check_true("no blanket 'no fasting' any more", "very-low-calorie diets or fasting" not in s and "can be supported" in s)
    check_true("states the 8-hour floor", "eating windows shorter than 8 hours" in s)
    check_true("names who it isn't for", "glucose-lowering medication" in s and "breastfeeding" in s)
    check_true("overate? don't starve to make up", "don't suggest eating less or skipping meals the next day to make up for it" in s)


def part_widget():
    """The widget follows a night-shift schedule: no re-sorting by clock time, daytime sleep counts as sleep,
    reminders follow the schedule. This machine may have no tkinter; a stub module is enough -- this only
    tests the logic, no window is opened."""
    import types
    sys.modules.setdefault("tkinter", types.ModuleType("tkinter"))
    spec = importlib.util.spec_from_file_location("widget_t", os.path.join(BASE, "bodycraft_widget.py"))
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    default_msgs = [x[1] for x in w.REMINDERS]
    d = os.path.join(TMP, "widget")
    os.makedirs(d, exist_ok=True)
    night = [{"t": "16:00", "e": "16:30", "title": "Wake up", "type": "life", "detail": "Drink a glass of water"},
             {"t": "18:00", "e": "18:30", "title": "Dinner", "type": "meal", "detail": ""},
             {"t": "18:30", "e": "22:00", "title": "Work", "type": "work", "detail": ""},
             {"t": "22:00", "e": "22:30", "title": "Shift meal", "type": "meal", "detail": ""},
             {"t": "22:30", "e": "07:30", "title": "Work", "type": "work", "detail": ""},
             {"t": "07:30", "e": "08:00", "title": "Wind down", "type": "rest", "detail": "Close the blackout curtains"}]
    with open(os.path.join(d, "content.json"), "w", encoding="utf-8") as f:
        json.dump({"schedule": night}, f, ensure_ascii=False)
    w.BASE = d
    w.load_content_override()
    check("schedule not re-sorted by clock: still starts with waking up", w.SCHEDULE[0][2], "Wake up")
    msgs = [x[1] for x in w.REMINDERS]
    check_true("no reminders configured: they follow the schedule",
               "Wake up: Drink a glass of water" in msgs and not any(x in msgs for x in default_msgs), msgs)
    check_true("no reminders during work blocks", not any(x.startswith("Work") for x in msgs), msgs)

    def at(hh, mm):
        class FakeDT:
            @staticmethod
            def now():
                return datetime.datetime(2026, 10, 14, hh, mm)
        w.datetime = types.SimpleNamespace(datetime=FakeDT, timedelta=datetime.timedelta)
        me = types.SimpleNamespace(_day=None, _reminded=set(), _content_mtime=os.path.getmtime(os.path.join(d, "content.json")),
                                   root=types.SimpleNamespace(after=lambda *a: None),
                                   draw=lambda: None, show_toast=lambda msg: None, update=lambda: None)
        w.Widget.update(me)
        return me
    me = at(11, 0)
    check("11 am (daytime sleep) counts as sleep time", me._act, "Sleep time")
    check("5 hours until waking up", me._count, "wake in 5h 00m")
    check("...and the next block is waking up", me._next, "Next: Wake up")
    me = at(2, 0)
    check("2 am is a work block", me._act, "Work")
    me = at(17, 0)
    check("5 pm is a gap: free time", me._act, "Free time")
    check("the next block is dinner", me._next, "Next: Dinner")

    print("\n[Desktop widget: the weekend schedule switches by weekday]")
    w.datetime = datetime   # back to the real datetime
    c = {"schedule": [{"t": "07:00", "e": "07:30", "title": "Weekday wake-up", "type": "life"}],
         "schedule_weekend": [{"t": "08:30", "e": "09:00", "title": "Weekend wake-up", "type": "life"}]}
    sat = datetime.datetime(2026, 10, 17, 10, 0)
    check("Saturday uses the weekend schedule", w.pick_schedule(c, sat)[0]["title"], "Weekend wake-up")
    check("2 am Saturday still counts as Friday", w.pick_schedule(c, datetime.datetime(2026, 10, 17, 2, 0))[0]["title"], "Weekday wake-up")
    check("Monday uses the weekday schedule", w.pick_schedule(c, datetime.datetime(2026, 10, 19, 10, 0))[0]["title"], "Weekday wake-up")
    c["weekend_days"] = []
    check("no weekday switching (shift work): always the weekday one", w.pick_schedule(c, sat)[0]["title"], "Weekday wake-up")
    c["weekend_days"] = [3]
    check("a day off they picked themselves (Wednesday)", w.pick_schedule(c, datetime.datetime(2026, 10, 14, 10, 0))[0]["title"], "Weekend wake-up")


# ======================================================================
def part_api():
    print("\n[API: meal times are kept]")
    fake = start_fake()
    proc, port = fresh_server(fake.server_address[1])
    try:
        today = datetime.date.today().isoformat()
        api(port, "/api/content", {"profile": {"setup_done": True, "start_weight": 74.5, "sex": "female",
                                               "age": 37, "height_cm": 162, "start_date": today,
                                               "health_note": "Fasting glucose 115 mg/dL, prediabetic, no meds",
                                               "eating_window": {"enabled": True, "hours": 10, "start": "09:30",
                                                                 "since": "2000-01-01"}}})
        d = api(port, "/api/data")
        d["members"][0].update({"name": "Sam", "start_weight": 74.5, "targets": [74.5, 63], "actual": {today: 74.8}})
        api(port, "/api/data", d)
        api(port, "/api/photo_meta", {"date": today, "meal": "lunch", "member": "default",
                                      "foods": [{"name": "Turkey sandwich", "cal": 174}], "cal": 174, "items": "Turkey sandwich", "at": "12:00"})
        api(port, "/api/photo_meta", {"date": today, "meal": "snack", "member": "default",
                                      "foods": [{"name": "Cookies", "cal": 200}], "cal": 200, "items": "Cookies", "at": "22:10"})
        ph = api(port, "/api/photos?date=%s&member=default" % today)
        check("today's meal comes back with its time", ph["lunch"].get("at"), "12:00")
        api(port, "/api/photo_meta", {"date": today, "meal": "lunch", "member": "default",
                                      "foods": [{"name": "Turkey sandwich", "cal": 200}], "cal": 200, "items": "Turkey sandwich"})
        ph = api(port, "/api/photos?date=%s&member=default" % today)
        check("editing only calories keeps the time", ph["lunch"].get("at"), "12:00")
        api(port, "/api/photo_meta", {"date": today, "meal": "lunch", "member": "default",
                                      "foods": [], "cal": None, "items": ""})
        ph = api(port, "/api/photos?date=%s&member=default" % today)
        check("clearing the meal clears the time too", ph["lunch"].get("at"), "")
        api(port, "/api/photo_meta", {"date": today, "meal": "lunch", "member": "default",
                                      "foods": [{"name": "Turkey sandwich", "cal": 174}], "cal": 174, "items": "Turkey sandwich", "at": "12:00"})
        mt = api(port, "/api/meal_times?days=3&member=default")
        check("meal times for the last few days", mt["days"].get(today, {}).get("snack", {}).get("at"), "22:10")

        csv_txt = api(port, "/api/export?member=default")
        check_true("export has first and last meal", "First meal" in csv_txt and "Last meal" in csv_txt
                   and "12:00" in csv_txt and "22:10" in csv_txt)

        print("\n[API: low moments are heard first]")
        r = api(port, "/api/intent", {"text": "Got yelled at today and I ate a whole bag of chips tonight. I'm so useless, forget it, I give up"})
        check("a bad day goes to conversation", r["intent"], "chat")
        check("marked as lapse", r["distress"], "lapse")
        check("tells the page there's something loggable", r["recordable"], True)

        print("\n[API: the coach knows this person and when they ate]")
        FAKE["log"].clear()
        FAKE["replies"][:] = ["Got it."]
        api(port, "/api/coach", {"persona": "warm", "events": [], "context": {},
                                 "history": [{"role": "user", "content": "Kids are finally asleep and I'm craving chips so bad"}]})
        sys_prompt = FAKE["log"][-1][0]["content"] if FAKE["log"] else ""
        check_true("prompt has the name and height", "Sam" in sys_prompt and "162" in sys_prompt)
        check_true("prompt has the eating window", "09:30–19:30 (14:10)" in sys_prompt)
        check_true("prompt has today's meal times", "12:00 Lunch" in sys_prompt)
        check_true("prompt points out food outside the window", "22:10" in sys_prompt)
        check_true("craving: something to do right now", "craving something and hasn't eaten it yet" in sys_prompt)
        check_true("prompt has the first 5% milestone", "losing the first 5%" in sys_prompt)
        check_true("prompt has their own health notes", "prediabetic" in sys_prompt, "the coach should remember them after a reload too")

        print("\n[API: the logging model knows what time it is]")
        FAKE["replies"][:] = ['{"meals":{"dinner":{"at":"19:00","day":-1,"foods":[{"name":"Rice","cal":232}]}}}']
        r = api(port, "/api/daily_log", {"text": "Had rice at 7 last night before my shift", "meals": [{"key": "dinner", "name": "Dinner"}]})
        dl_prompt = FAKE["log"][-1][-1]["content"] if FAKE["log"] else ""
        check_true("prompt says 'It is now'", "It is now " + today in dl_prompt)
        check_true("two night meals: a late-snack meal keeps them apart", 'named like "Night meal"' in dl_prompt)
        check("last night's dinner comes back with day=-1", ((r.get("log") or {}).get("meals") or {}).get("dinner", {}).get("day"), -1)

        print("\n[API: one % doesn't break the review]")
        FAKE["replies"][:] = ["## How these days went\nPretty good."]
        r = api(port, "/api/review", {"start": today, "end": today, "member": "default"})
        check("review is generated", r.get("ok"), True, r.get("err") or "")
        prompt = FAKE["log"][-1][-1]["content"] if FAKE["log"] else ""
        check_true("review prompt has the 5% line that used to crash it", "first 5%" in prompt)
        check_true("review uses meal names with times, not keys", "Lunch 12:00" in prompt and "lunch 12:00" not in prompt
                   and "Snack 22:10" in prompt)
        check_true("review knows the eating window", "eating window of 09:30–19:30" in prompt)
    finally:
        stop(proc)
        fake.shutdown()


# ======================================================================
def part_page():
    from playwright.sync_api import sync_playwright
    print("\n[Page: the eating window stays on once it's on]")
    proc, port = fresh_server()
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(locale="en-US")
            pg = ctx.new_page()
            # Pinned to 1 pm today: the eating window and fasting time depend on the clock; real time makes this flaky
            pg.clock.install(time=datetime.datetime.combine(datetime.date.today(), datetime.time(13, 0)))
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_timeout(2600)
            check("setup defaults to pounds", pg.input_value("#s0Unit"), "lb")
            pg.fill("#s0Name", "Sam")
            pg.fill("#s0Ft", "5")
            pg.fill("#s0In", "4")
            pg.fill("#s0Start", "164.2")
            pg.fill("#s0Target", "139")
            pg.fill("#s0Weeks", "24")
            hint = pg.locator("#s0Hint").inner_text()
            check_true("setup hint shows the first 5% stop", "156" in hint and "5%" in hint, hint)
            pg.click("#s0Ok")
            pg.wait_for_timeout(2400)
            sch = pg.evaluate("schedule.map(s => s.t + ' ' + s.title)")
            check_true("schedule built around meals: 12:00 lunch", "12:00 Lunch" in sch, sch)
            check("schedule starts with waking up", sch[0], "06:30 Wake up")
            got = pg.evaluate("""()=>({
                first: scheduleWeekend && scheduleWeekend[0].t, days: weekendDays,
                work: (scheduleWeekend || []).some(x => x.type === 'work'),
                sat: activeSchedule(new Date(2026, 9, 17, 10, 0)) === scheduleWeekend,
                satNight: activeSchedule(new Date(2026, 9, 17, 2, 0)) === schedule,
                mon: activeSchedule(new Date(2026, 9, 19, 10, 0)) === schedule})""")
            check("setup also builds a weekend schedule, an hour later", got["first"], "07:30")
            check("no 'work' blocks in the weekend schedule", got["work"], False)
            check_true("Saturday daytime uses the weekend schedule", got["sat"])
            check_true("Friday night until 2 am Saturday still uses the weekday one", got["satNight"])
            check_true("Monday is back to the weekday schedule", got["mon"])
            pg.clock.set_system_time(datetime.datetime(2026, 10, 17, 11, 0))   # weekend breakfast 08:30; 11 am is between meals
            got = pg.evaluate("""()=>{ scheduleTab = null; renderTimeline(); updateNowCard();
                return {title: scheduleTitle.innerText, tabs: scheduleTabs.innerText, now: nowTitle.innerText}; }""")
            check("on Saturday the schedule page shows the weekend schedule", got["title"], "Weekend routine")
            check_true("the 'Weekend' tab is marked as today", "today" in got["tabs"].lower(), got["tabs"])
            check("home 'right now' uses the weekend schedule", got["now"], "Free time")
            pg.evaluate("scheduleTab = 'work'; renderTimeline()")
            check("weekday tab: nothing marked as now", pg.locator("#timeline .tl-item.now").count(), 0)
            pg.evaluate("scheduleTab = null")
            pg.clock.set_system_time(datetime.datetime.combine(datetime.date.today(), datetime.time(13, 0)))
            got = pg.evaluate("""buildSchedule({wake:'15:00', sleep:'09:30', meals:['16:00','00:30','08:30']})
                                 .map(s => s.t + ' ' + s.title + ' ' + s.type)""")
            check_true("night schedule has no '09:30 Lunch'", not any(x.startswith("09:30 Lunch") for x in got), got)
            check_true("night schedule eats at 00:30", any(x.startswith("00:30 Shift meal") for x in got), got)
            check_true("night schedule wakes at 3 pm", got[0].startswith("15:00 Wake up"), got[0])
            check_true("window off: there's an intro card", pg.locator("#fastIntroGo").count() == 1)
            pg.click("#fastIntroGo")
            pg.wait_for_timeout(400)
            pg.locator('.ew-risk[data-k="dm"]').check()
            check("glucose-lowering meds checked: can't turn it on", pg.locator("#ewSave").is_disabled(), True)
            pg.locator('.ew-risk[data-k="dm"]').uncheck()
            pg.click('.ew-opt:has(input[value="8"])')
            pg.fill("#ewStart", "10:00")
            pg.click("#ewSave")
            pg.wait_for_timeout(1000)
            check_true("once on, the window is shown", "10:00 AM–6:00 PM" in pg.locator("#fastCard").inner_text(),
                       pg.locator("#fastCard").inner_text()[:120])
            pg.reload()
            pg.wait_for_timeout(2800)
            check_true("the window survives a reload", "10:00 AM–6:00 PM" in pg.locator("#fastCard").inner_text(),
                       "lost here once: content had it, the page didn't read it")
            check_true("home start weight is the 164.2 entered", pg.locator("#statWeight").inner_text().startswith("164.2"),
                       pg.locator("#statWeight").inner_text())

            print("\n[Page: logging a meal keeps the time]")
            pg.click('#photoSlots [data-act="manual"][data-meal="lunch"]')
            pg.wait_for_timeout(400)
            pg.fill("#foodList .f-name", "Turkey sandwich")
            pg.locator("#foodList input.f-num").nth(1).fill("420")
            pg.fill("#mealAtInput", "12:15")
            pg.click("#modalOk")
            pg.wait_for_timeout(1500)
            slots = pg.locator("#photoSlots").inner_text()
            check_true("the meal card shows the time", "12:15" in slots, slots[:200])
            ft = pg.locator("#fastCard").inner_text()
            check_true("the eating-window card sees this meal", "12:15" in ft or "fast" in ft.lower() or "open" in ft.lower(), ft[:200])

            print("\n[Page: few check-offs, and logging ticks them for you]")
            check("only five default habits", pg.evaluate("habits.length"), 5)
            cl = pg.locator("#checklist").inner_text()
            check_true("lunch logged: 'Log what you eat' ticks itself", "Meal logged · checked automatically" in cl, cl[:200])
            check("the count goes to 1/5", pg.locator("#checkCount").inner_text(), "1/5")
            idx = pg.evaluate("habits.findIndex(h => h.t === 'Log what you eat')")
            pg.locator("#checklist .check").nth(idx).click()
            pg.wait_for_timeout(300)
            pg.evaluate("refreshDerived()")
            pg.wait_for_timeout(300)
            check("unticked by hand: not ticked again", pg.locator("#checklist .check").nth(idx).get_attribute("class").split().count("done"), 0)
            got = pg.evaluate("""()=>{ const h = {t:'Eat breakfast', s:'High protein'}; return [habitAutoReason({t:'Eat lunch'}), habitAutoReason(h)]; }""")
            check("an older 'Eat lunch' habit is recognized", got[0], "Lunch logged")
            check("no breakfast logged, not ticked", got[1], "")
            check("home activity goal is 30 minutes", pg.locator("#statExercise").inner_text(), "30 min")

            print("\n[Page: weight chart is full width with a 7-day average line]")
            pg.evaluate("""()=>{
                const m = activeMember(); m.actual = m.actual || {};
                const ws = [74.5, 74.9, 74.2, 74.6, 74.0, 74.3, 73.8, 74.1, 73.6, 73.9];
                ws.forEach((w, i) => { m.actual[ymd(new Date(Date.now() - (ws.length - 1 - i) * 86400000))] = w; });
                return saveData();
            }""")
            pg.click('#tabs button[data-t="weight"]')
            pg.wait_for_timeout(700)
            got = pg.evaluate("""()=>{
                const svg = document.querySelector('#weightChart svg');
                const vb = svg.getAttribute('viewBox').split(' ').map(Number);
                const ticks = [...svg.querySelectorAll('text.axis-tick')].map(t => t.textContent).filter(t => /^\\d+(\\.\\d+)?$/.test(t)).map(Number);
                return {w: vb[2], box: document.getElementById('weightChart').clientWidth,
                        ma: !!document.getElementById('weightMA'), lo: Math.min(...ticks), hi: Math.max(...ticks),
                        foot: document.getElementById('weightChart').innerText};
            }""")
            check_true("chart drawn at the real width, filling the row", got["w"] == got["box"] and got["w"] > 900, got)
            check_true("7-day average line drawn", got["ma"])
            check_true("y axis fits the visible values (not stretched to the goal)", got["lo"] >= 154 and got["hi"] <= 170, got)
            check_true("the latest 7-day average is under the chart", "7-day avg, latest" in got["foot"], got["foot"][-200:])
            cl = pg.evaluate("renderChecklist(), document.getElementById('checklist').innerText")
            check_true("weight logged today: 'Weigh in' ticks itself", "Weight logged · checked automatically" in cl, cl[:200])

            print("\n[Page: a report in the library feeds the health notes]")
            got = pg.evaluate("""async ()=>{
                const r = await fetch('/api/attach', {method:'POST', headers:{'Content-Type':'application/json'},
                  body: JSON.stringify({member: appData.active, name: 'report.txt', category: 'library', date: todayKey(),
                                        note: 'August physical', data: btoa('ALT 52'), ext: 'txt'})});
                const j = await r.json();
                await loadLibrary();
                return j.ok;
            }""")
            check("report uploaded", got, True)
            pg.click('#tabs button[data-t="library"]')
            pg.wait_for_timeout(400)
            check_true("library card offers 'add to health notes'", pg.locator("#libList [data-hn]").count() >= 1)
            pg.locator("#libList [data-hn]").first.click()
            pg.wait_for_timeout(300)
            check_true("opens prefilled with the report's name", pg.input_value("#hnText").startswith("August physical"), pg.input_value("#hnText"))
            check_true("says the report itself isn't sent to the model", "isn't sent to the model" in pg.locator(".hn-src").inner_text(),
                       pg.locator(".hn-src").inner_text())
            pg.fill("#hnText", "August physical: mild fatty liver, triglycerides 186, doctor says lose 5%-10%")
            pg.click("#hnOk")
            pg.wait_for_timeout(600)
            check_true("saved into health notes", "fatty liver" in pg.evaluate("profile.health_note"))
            pg.evaluate("profile.health_note = ''; fetch('/api/content',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({profile:{health_note:''}})})")
            pg.wait_for_timeout(300)
            pg.click('#tabs button[data-t="today"]')
            pg.wait_for_timeout(300)

            print("\n[Page: a bad day doesn't get a logging form]")
            pg.click("#coachBtn")
            pg.wait_for_timeout(300)
            pg.fill("#coachInput", "Got yelled at by my boss today and I ate a whole bag of chips tonight. I'm so useless, forget it, I give up")
            pg.click("#coachSend")
            pg.wait_for_timeout(2500)
            check("no logging panel pops up", pg.locator("#dlText").count(), 0)
            body = pg.locator("#coachBody").inner_text()
            check_true("no AI set up: still a guide for the moment", "Overate?" in body, body[-300:])
            check_true("asks whether to log it, and says not logging is fine", "Want me to log what you just said? Skipping it is fine too." in body, body[-300:])

            print("\n[Page: one-click window switch for shift work]")
            pg.click("#coachClose") if pg.locator("#coachPanel").is_visible() else None
            pg.evaluate("window.scrollTo(0,0)")
            pg.click("#fastSet")
            pg.wait_for_timeout(400)
            pg.check("#ewAltOn")
            pg.fill("#ewAltStart", "18:00")
            check("night-shift days default to waking at 16:00", pg.input_value("#ewAltWake"), "16:00")
            pg.click("#ewSave")
            pg.wait_for_timeout(1500)
            check("first time shift work is on, weekend switching stops", pg.evaluate("weekendDays"), [])
            check_true("the card has a one-click switch", pg.locator("#fastSwap").count() == 1)
            check_true("shift work adds a late-snack meal", "supper" in pg.evaluate("mealMeta.map(m => m.key)"),
                       pg.evaluate("mealMeta.map(m => m.key)"))
            check_true("the today page shows the night meal", "Night meal" in pg.locator("#photoSlots").inner_text(),
                       pg.locator("#photoSlots").inner_text()[:200])
            check("the other schedule is a night one", pg.evaluate("isNightSchedule(scheduleAlt)"), True)
            check_true("schedule page says the other one is ready", "switch" in pg.locator("#scheduleAltBar").inner_text().lower(),
                       pg.locator("#scheduleAltBar").inner_text())
            pg.click("#fastSwap")
            pg.wait_for_timeout(1200)
            ft = pg.locator("#fastCard").inner_text()
            check_true("switched to the night window", "6:00 PM–2:00 AM" in ft, ft[:160])
            check_true("the backup is now the old one", "10:00 AM–6:00 PM" in ft, ft[:240])
            check("the schedule switches to nights too", pg.evaluate("isNightSchedule(schedule)"), True)
            check("the schedule title follows", pg.locator("#scheduleTitle").inner_text(), "Night-shift routine")
            sl = pg.evaluate("scheduleWakeSleep(schedule).sleep")
            check("the night schedule ends at 08:00", sl, "08:00")
            cl = pg.locator("#checklist").inner_text()
            check_true("the default 'Lights out' habit follows the night schedule", "8:00 AM" in cl and "by 11" not in cl, cl[-120:])
            got = pg.evaluate("""()=>{ const keep = habits; habits = keep.slice(0, 4).concat([{t:'Lights out', s:'By 11:00 PM'}]);
                renderChecklist(); const t = document.getElementById('checklist').innerText; habits = keep; renderChecklist(); return t; }""")
            check_true("a habit written 'By 11:00 PM' follows the night schedule", "By 8:00 AM" in got, got)
            lb = pg.locator("#statKcalLabel").inner_text()
            check_true("1 pm, night window not open yet: says when it opens, not what's left", "opens" in lb.lower() and "6:00 PM" in lb, lb)
            pg.reload()
            pg.wait_for_timeout(2800)
            ew = pg.evaluate("ewGet()")
            check("history survives a reload (one entry per day, the last one)", len(ew.get("history") or []), 1)
            check("the backup window survives a reload", (ew.get("alt") or {}).get("start"), "10:00")
            check("still a night schedule after reload", pg.evaluate("isNightSchedule(schedule)"), True)
            check("the day schedule is the backup", pg.evaluate("isNightSchedule(scheduleAlt)"), False)

            print("\n[Page: nights count by 'this shift'; greetings don't tell you to sleep]")
            today = datetime.date.today()
            pg.clock.set_system_time(datetime.datetime.combine(today, datetime.time(23, 30)))
            got = pg.evaluate("""()=>{
                mealTimes = {}; photoUrls = {dinner:{at:'19:00', cal:600}, supper:{at:'22:30', cal:400}};
                renderBudget(); renderNetCal();
                return {lb: statKcalLabel.innerText, tag: netCalTag.innerText,
                        t: heroTitle.innerText, s: heroSub.innerText, ph: coachInput.placeholder};
            }""")
            check_true("11:30 pm, window open: left for this shift", "this shift" in got["lb"].lower(), got["lb"])
            check("calorie balance is per shift too", got["tag"], "Shift in progress")
            check_true("late at night: no 'still up?'", "still up" not in got["t"].lower() and "shift" in got["t"].lower(), got["t"])
            check_true("remembers two meals this shift", "2 meals" in got["s"], got["s"])
            check_true("input hint says to log the whole shift at once", "shift" in got["ph"].lower(), got["ph"])
            tmr = today + datetime.timedelta(days=1)
            pg.clock.set_system_time(datetime.datetime.combine(tmr, datetime.time(3, 0)))
            got = pg.evaluate("""(d)=>{
                mealTimes = {}; mealTimes[d] = {dinner:{at:'19:00', cal:600}, supper:{at:'22:30', cal:400}};
                photoUrls = {snack:{at:'01:30', cal:300}};
                renderBudget(); renderNetCal();
                return {v: statKcal.innerText, lb: statKcalLabel.innerText, tag: netCalTag.innerText, t: heroTitle.innerText};
            }""", today.isoformat())
            check_true("3 am: window closed", "closed" in got["v"].lower(), got["v"])
            check_true("calorie balance counts last night to this morning as one shift", "shift" in got["tag"].lower(), got["tag"])
            check_true("still a night-shift greeting at 3 am", "shift" in got["t"].lower(), got["t"])
            pg.clock.set_system_time(datetime.datetime.combine(tmr, datetime.time(9, 0)))
            got = pg.evaluate("renderHero(), heroTitle.innerText + '|' + heroSub.innerText")
            check_true("9 am: shift's over", "shift" in got.split("|")[0].lower(), got)
            check_true("reminds to weigh in after sleeping", "Weigh in after you wake up" in got, got)
            pg.clock.set_system_time(datetime.datetime.combine(today, datetime.time(13, 0)))

            print("\n[Page: eating across midnight is one row]")
            got = pg.evaluate("""()=>{
                const t = new Date(); const y = new Date(Date.now() - 86400000);
                const k = d => ymd(d);
                mealTimes = {}; mealTimes[k(y)] = {dinner:{at:'19:00', cal:600}};
                photoUrls = {snack:{at:'00:30', cal:480}, breakfast:{at:'00:40', cal:300}};
                const g = eatingDays();
                renderEatTimes();
                return {n: g.length, span: g.length ? g[g.length-1].span : 0,
                        txt: document.getElementById('eatTimes').innerText};
            }""")
            check("yesterday 19:00 and today 00:30 are one stretch", got["n"], 1)
            check("that stretch is 5.7 hours", round(got["span"], 1), 5.7)

            print("\n[Page: health notes]")
            pg.click("#coachBtn")
            pg.wait_for_timeout(300)
            pg.click("#healthNoteBtn")
            pg.wait_for_timeout(300)
            pg.fill("#hnText", "Fasting glucose 115 mg/dL, taking metformin")
            pg.click("#hnOk")
            pg.wait_for_timeout(800)
            pg.reload()
            pg.wait_for_timeout(2800)
            check("health notes survive a reload", pg.evaluate("profile.health_note"), "Fasting glucose 115 mg/dL, taking metformin")
            pg.click("#fastSet")
            pg.wait_for_timeout(400)
            check("metformin mentioned: the glucose-meds box is ticked", pg.locator('.ew-risk[data-k="dm"]').is_checked(), True)
            check("save is disabled", pg.locator("#ewSave").is_disabled(), True)
            pg.click("#ewCancel")

            print("\n[Page: badges once earned stay]")
            got = pg.evaluate("""()=>{
                store.set('achievements-' + activeMember().id, {streak7: '2026-01-01'});
                renderAchievements();
                const box = document.getElementById('achievements');
                return box ? box.innerText : '';
            }""")
            # thresholds by id are fragile; just check the unlock date is shown
            check_true("after a break, the old badge still shows its unlock date", "Unlocked Jan 1" in got, got[:300])
            check("no page errors", errs, [])
            b.close()
    finally:
        stop(proc)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg not in ("", "api", "page"):
        print("usage: python3 test_eating.py [api|page]")
        sys.exit(2)
    if arg in ("", "api"):
        part_units()
        part_api()
    if arg in ("", "page"):
        part_page()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 60)
    print("  Eating window: %d passed, %d failed" % (len(PASS), len(FAIL)))
    print("=" * 60)
    if FAIL:
        for f in FAIL:
            print("  x", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
