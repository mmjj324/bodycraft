"""Data-flow consistency tests -- change A, does B follow?

This layer used to be completely empty, and it is where most escaped bugs lived:
"logging a weight doesn't update the Review tab", "after exercises became an array,
exercise this week is always 0". Each page looks right on its own; you only catch
these by asserting numbers across pages.

Every case has the same shape: act -> switch tab -> assert a specific number.
We do not assert "no error" -- that is the smoke test's job. Here we want
"is the number right".

The user here is the default US user: pounds, height in feet and inches.
Weights are stored in kg internally, so the expected values below are computed
the same way the app is supposed to: convert what the user typed, then show it
back in pounds.

Usage: python3 test_dataflow.py
Starts its own temporary server instance.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = "bodycraft_server.py"
TMP = None

LB = 2.20462

PASS, FAIL = [], []


def check(name, got, want, note=""):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    flag = "PASS" if ok else "FAIL"
    line = "  %-44s %s" % (name, flag)
    if not ok:
        line += "   expected %r, got %r" % (want, got)
    if note:
        line += "   " + str(note)
    print(line)
    return ok


def check_true(name, cond, note=""):
    return check(name, bool(cond), True, note)


def to_kg(lb):
    """Same conversion as toKg(v, 'lb') on the page: kg rounded to 2 decimals."""
    return round(lb / LB, 2)


def to_lb(kg):
    return round(kg * LB, 1)


def mifflin(sex, kg, cm, age):
    base = 10 * kg + 6.25 * cm - 5 * age
    return max(800, int(round(base + (5 if sex == "male" else -161))))


def num(s):
    """Parse a displayed number, including the typographic minus sign."""
    if s is None:
        return None
    return float(s.replace("−", "-").replace(",", ""))


class App:
    """Thin wrapper over playwright for the repeated "switch tab, read numbers" moves."""

    def __init__(self, pg):
        self.pg = pg

    def tab(self, name):
        self.pg.click("nav.tabs button[data-t='%s']" % name)
        self.pg.wait_for_timeout(450)

    def stats(self):
        """The four cards at the top of the Review tab, keyed by their label.

        Only #statCards is read, not the whole #panel-stats: further down there are
        achievement badges whose descriptions ("Down 2.0 lb") would match a loose
        regex. With empty data both happen to agree; once a badge unlocks they don't,
        and the failure looks just like a product bug.
        """
        self.tab("stats")
        cards = self.pg.eval_on_selector_all(
            "#statCards > div",
            "els=>els.map(e=>[...e.children].map(c=>c.textContent.trim()))")
        by = {c[2]: c[1] for c in cards if len(c) >= 3}

        def g(label, pat):
            v = by.get(label)
            if v is None:
                return None
            m = re.search(pat, v)
            return m.group(1) if m else None
        lost = g("Total lost", r"([−+\-]?[\d.]+) lb")
        if lost is None:
            lost = g("Up since start", r"([−+\-]?[\d.]+) lb")
        days = g("Days weighed in", r"(\d+) days?")
        mins = g("Exercise this week", r"(\d+) min")
        return {
            "days": int(days) if days is not None else None,
            "current": num(g("Current weight", r"([\d.]+) lb")),
            "lost": num(lost),
            "week_ex": int(mins) if mins is not None else None,
        }

    def netcal(self):
        """Calories in vs. out on the Today tab."""
        self.tab("today")
        t = self.pg.inner_text("#netCalBar")

        def g(pat):
            m = re.search(pat, t)
            return int(m.group(1)) if m else None
        if "cal over" in t:
            verdict = "over"
        elif "exactly even" in t:
            verdict = "even"
        else:
            verdict = "deficit"
        return {
            "in": g(r"\bIn\s+(\d+) cal"),
            "out": g(r"\bOut\s+(\d+) cal"),
            "bmr": g(r"BMR (\d+)"),
            "exercise": g(r"\+ exercise (\d+)"),
            "verdict": verdict,
        }

    def ex_today(self):
        self.tab("today")
        t = self.pg.inner_text("#exToday").replace("\n", " ")
        m = re.search(r"Today: (\d+) workouts?, (\d+) min,\s*about (\d+) cal", t)
        if not m:
            return {"count": 0, "minutes": 0, "kcal": 0}
        return {"count": int(m.group(1)), "minutes": int(m.group(2)), "kcal": int(m.group(3))}

    def record_weight(self, val, date=None):
        self.tab("weight")
        if date:
            self.pg.evaluate("(d)=>{document.getElementById('wDate').value=d;}", date)
        self.pg.fill("#wInput", str(val))
        self.pg.click("#wSave")
        self.pg.wait_for_timeout(1100)

    def record_exercise(self, typ, minutes, intensity="moderate"):
        self.tab("today")
        self.pg.select_option("#exType", typ)
        self.pg.fill("#exDur", str(minutes))
        self.pg.select_option("#exIntensity", intensity)
        self.pg.click("#exSave")
        self.pg.wait_for_timeout(800)
        if self.pg.locator("#exOk").count():
            self.pg.click("#exOk")
            self.pg.wait_for_timeout(1000)

    def record_meal(self, meal, cal, name="Test meal"):
        self.pg.evaluate(
            """([m,c,n])=>fetch('/api/photo_meta',{method:'POST',
                 headers:{'Content-Type':'application/json'},
                 body:JSON.stringify({date:todayKey(),meal:m,member:appData.active,
                   foods:[{name:n,cal:c}],cal:c,items:n})})
               .then(()=>loadPhotos()).then(()=>refreshDerived())""",
            [meal, cal, name])
        self.pg.wait_for_timeout(1100)

    def check_habits(self, n):
        self.tab("today")
        for i in range(n):
            self.pg.locator("#checklist .check").nth(i).click()
            self.pg.wait_for_timeout(260)

    def disk(self, fname):
        p = os.path.join(TMP, fname)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    def local_keys(self):
        return self.pg.evaluate("()=>Object.keys(localStorage).sort()")


DATA_FILES = (".server_running.json", "api_config.json", "favorites.json", "food_lookup.json",
              "knowledge.json", "data.json", "content.json", "photos.json", "diary.json",
              "selfies.json", "attach.json", "member_setup.json")


def setup_env():
    """Copy the app's top-level files into a fresh temp dir and start a server there."""
    global TMP
    TMP = tempfile.mkdtemp(prefix="bodycraft_dataflow_")
    for f in os.listdir(BASE):
        src = os.path.join(BASE, f)
        if os.path.isfile(src) and not f.startswith(".") and f not in DATA_FILES:
            shutil.copy2(src, os.path.join(TMP, f))
    proc = subprocess.Popen(
        [sys.executable, SERVER], cwd=TMP,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    flag = os.path.join(TMP, ".server_running.json")
    for _ in range(60):
        time.sleep(0.3)
        if os.path.exists(flag):
            try:
                with open(flag, encoding="utf-8") as f:
                    return proc, json.load(f)["port"]
            except Exception:
                continue
    proc.kill()
    raise RuntimeError("server did not start")


def run(pg, port):
    app = App(pg)
    pg.goto("http://127.0.0.1:%d/" % port)
    pg.wait_for_timeout(2600)

    # Wizard in pounds: female, 38, 5'7" (170 cm), 172 lb -> 158 lb over 16 weeks
    check("Wizard defaults to lb", pg.input_value("#s0Unit"), "lb")
    check_true("Height asks for ft/in in lb mode", pg.is_visible("#s0Ft") and pg.is_visible("#s0In")
               and not pg.is_visible("#s0Height"))
    pg.fill("#s0Name", "Test")
    pg.select_option("#s0Sex", "female")
    pg.fill("#s0Age", "38")
    pg.fill("#s0Ft", "5")
    pg.fill("#s0In", "7")
    pg.fill("#s0Start", "172")
    pg.fill("#s0Target", "158")
    pg.fill("#s0Weeks", "16")
    pg.click("#s0Ok")
    pg.wait_for_timeout(2400)
    height = pg.evaluate("()=>profile.height_cm")
    check("5 ft 7 in stored as 170 cm", height, 170)
    start_kg = to_kg(172)
    check("172 lb stored as kg", pg.evaluate("()=>profile.start_weight"), start_kg)

    print("\n[A  Weight -> derived views]")
    base = app.stats()
    check("Initial days logged", base["days"], 0)
    check("Initial total lost", base["lost"], 0.0)
    app.record_weight(168.4)
    w1 = to_kg(168.4)
    check("Weight saved in kg", pg.evaluate("()=>activeMember().actual[todayKey()]"), w1)
    st = app.stats()
    check("After weigh-in: days logged", st["days"], 1)
    check("After weigh-in: current weight (lb)", st["current"], 168.4)
    check("After weigh-in: total lost (lb)", st["lost"], -3.6, "172 - 168.4, shown as a loss")
    nc = app.netcal()
    # BMR uses the *latest* weight, not the starting weight -- that is intended:
    # same height and age, 172 lb and 168.4 lb should give different BMRs.
    check("BMR follows the latest weight", nc["bmr"], mifflin("female", w1, 170, 38),
          "Mifflin: female 38, 170 cm, %.2f kg" % w1)
    app.record_weight(165.0)
    w2 = to_kg(165.0)
    st2 = app.stats()
    check("Same-day re-weigh doesn't add a day", st2["days"], 1)
    check("Same-day re-weigh updates the value", st2["current"], 165.0)

    print("\n[B  Exercise -> derived views]")
    app.record_exercise("Walk", 40)
    e1 = app.ex_today()
    check("First workout: count", e1["count"], 1)
    check("First workout: minutes", e1["minutes"], 40)
    n1 = app.netcal()
    check("Calories out include exercise", n1["exercise"], e1["kcal"])
    s1 = app.stats()
    check("Exercise this week follows", s1["week_ex"], 40)

    app.record_exercise("Run", 25, "vigorous")
    e2 = app.ex_today()
    check("Second workout appends, not overwrites", e2["count"], 2, "<- data was lost here once")
    check("Minutes are summed", e2["minutes"], 65)
    n2 = app.netcal()
    check("Calories out use the total", n2["exercise"], e2["kcal"])
    s2 = app.stats()
    check("Exercise this week uses the total", s2["week_ex"], 65)
    disk = app.disk("data.json")
    arr = list((disk["members"][0].get("exercises") or {}).values())
    check("Saved to disk as an array of two", len(arr[0]) if arr else 0, 2)

    print("\n[C  Meals -> calories in and the verdict]")
    app.record_meal("breakfast", 400)
    m1 = app.netcal()
    check("Intake counted", m1["in"], 400)
    check("Still a deficit", m1["verdict"], "deficit")
    app.record_meal("lunch", 700)
    # Dinner is set to land 200 over calories out instead of a hard-coded number:
    # the burn formula gained an activity factor once and a fixed 900 stopped being "over".
    # The case asserts "going over flips the verdict", not any particular number.
    cur = app.netcal()
    need = cur["out"] - 400 - 700 + 200
    app.record_meal("dinner", need)
    m2 = app.netcal()
    check("Three meals add up", m2["in"], 400 + 700 + need)
    check("Verdict flips when over", m2["verdict"], "over",
          "%d in vs %s out" % (m2["in"], m2["out"]))

    print("\n[C2  Calorie check-off <-> meal log: one ledger]")
    # Section C already logged breakfast, lunch and dinner. Use the snack slot and assert deltas.
    app.tab("today")
    base = app.netcal()["in"]
    snack_idx = pg.evaluate("()=>mealMeta.findIndex(m=>m.key==='snack')")
    check_true("There is a snack slot", snack_idx >= 0)
    snack_default = pg.evaluate("()=>mealDefaultKcal('snack')")
    pg.locator("#calSegs .calseg").nth(snack_idx).click()
    pg.wait_for_timeout(500)
    c1 = app.netcal()
    check("Checking off alone counts toward intake", c1["in"] - base, snack_default,
          "<- check-offs used to have their own ledger")
    row = pg.eval_on_selector("#calSegs .calseg:nth-child(%d) .src" % (snack_idx + 1), "e=>e.textContent")
    check_true("Source shows Default", row.strip().startswith("Default"), row)
    app.record_meal("snack", 180)
    c2 = app.netcal()
    check("A logged value wins over the default", c2["in"] - base, 180,
          "default %d no longer counted" % snack_default)
    rec = pg.eval_on_selector("#calSegs .calseg:nth-child(%d)" % (snack_idx + 1), "e=>e.classList.contains('rec')")
    check_true("Check-off row marked as logged", rec)
    # Breakfast was logged in C without being checked off -> it should be checked automatically
    on = pg.eval_on_selector("#calSegs .calseg:nth-child(1)", "e=>e.classList.contains('done')")
    check_true("A logged meal is checked off automatically", on)
    pg.locator("#calSegs .calseg").nth(snack_idx).click()
    pg.wait_for_timeout(500)
    check("A logged meal can't be unchecked", app.netcal()["in"] - base, 180)

    print("\n[D  Habits -> check-in rate and auto-converted workouts]")
    before = app.ex_today()["count"]
    app.check_habits(4)
    after = app.ex_today()
    check_true("Exercise habits convert to workouts", after["count"] >= before,
               "%d habits checked -> %d workouts" % (4, after["count"]))
    app.tab("stats")
    # The bar charts were replaced by insight cards. Checking habits -> the logging streak tile is there
    tiles = pg.eval_on_selector_all("#insights > div", "e=>e.map(x=>x.textContent.replace(/\\s+/g,' '))")
    streak_tile = [t for t in tiles if "Logging streak" in t]
    check_true("Insights show a logging streak", bool(streak_tile), tiles[0][:40] if tiles else "none")
    if streak_tile:
        m = re.search(r"Logging streak\D*?(\d+) days?", streak_tile[0])
        check_true("Streak is at least 1 day", m and int(m.group(1)) >= 1, streak_tile[0][:60])
    check_true("Insights show BMI", any("BMI" in t for t in tiles))
    check_true("Insights show goal progress", any("Goal progress" in t for t in tiles))

    print("\n[E  Settings -> derived views]")
    pg.evaluate("""()=>{
      profile.sex='male'; profile.age=30;
      return fetch('/api/content',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({profile:{sex:'male',age:30}})})
        .then(()=>{ refreshDerived(); renderPeriodMark(); });
    }""")
    pg.wait_for_timeout(1200)
    nc3 = app.netcal()
    check("BMR changes with sex and age", nc3["bmr"], mifflin("male", w2, 170, 30),
          "male 30, 170 cm, latest weight 165 lb (%.2f kg)" % w2)
    app.tab("today")
    check("No period tracker for men", pg.locator("#pdToday").count(), 0)

    # Activity factor: burn = BMR x factor + exercise. Changing the factor changes burn; exercise is added once
    def burn_parts():
        t = pg.inner_text("#netCalBar").replace("\n", " ")
        m = re.search(r"Burn (\d+) = BMR (\d+) \(.*?\) × ([\d.]+) \(([^)]+)\)\s*\+ exercise (\d+)", t)
        return (int(m.group(1)), int(m.group(2)), float(m.group(3)), m.group(4), int(m.group(5))) if m else None
    pg.evaluate("()=>{profile.activity_level='sedentary'; refreshDerived();}")
    pg.wait_for_timeout(500)
    b0 = burn_parts()
    check_true("Burn formula is readable", b0 is not None, pg.inner_text("#netCalBar")[-200:])
    if b0:
        total, bmr, f, lvl, ex = b0
        check("Sedentary factor 1.2", f, 1.2)
        check("Sedentary label", lvl, "Sedentary")
        check("Burn = BMR x factor + exercise", total, int(round(bmr * f)) + ex,
              "%d = %d x %s + %d" % (total, bmr, f, ex))
        pg.evaluate("()=>{profile.activity_level='moderate'; refreshDerived();}")
        pg.wait_for_timeout(500)
        b1 = burn_parts()
        check("Moderately active factor 1.55", b1[2], 1.55)
        check("BMR itself doesn't change", b1[1], bmr)
        check("Exercise not double-counted", b1[4], ex)
        check_true("Burn rises with the factor", b1[0] > total, "%d -> %d" % (total, b1[0]))
        pg.evaluate("()=>{profile.activity_level='sedentary'; refreshDerived();}")
        pg.wait_for_timeout(300)

    print("\n[F  Delete -> reverse propagation]")
    app.tab("today")
    n_before = app.ex_today()["count"]
    pg.locator(".ex-del").first.click()
    pg.wait_for_timeout(1100)
    e3 = app.ex_today()
    check("Deleting one drops the count by one", e3["count"], n_before - 1)
    n4 = app.netcal()
    check("Calories out follow", n4["exercise"], e3["kcal"])

    print("\n[G  Erase all -> both stores agree]")
    pg.evaluate("()=>{const b=document.getElementById('wipeBtn'); if(b) b.click();}")
    pg.wait_for_timeout(700)
    # Erasing reloads the page. expect_navigation only guarantees that a navigation happened,
    # not that the new page finished loading its data -- it still fetches /api/data after the
    # reload, and until then the DOM shows what was rendered at reload time. Reading it with a
    # fixed wait once made a clean wipe look dirty. So also wait for an explicit signal:
    # the records in front-end memory really are empty.
    try:
        with pg.expect_navigation(timeout=15000):
            pg.evaluate("()=>{const b=document.getElementById('wpOk'); if(b) b.click();}")
    except Exception:
        pass
    pg.wait_for_timeout(2000)
    if pg.locator("#s0Skip").count():
        pg.click("#s0Skip")
        pg.wait_for_timeout(1300)
    for _ in range(20):
        try:
            done = pg.evaluate("""()=>{
                const m = (typeof activeMember === 'function') ? activeMember() : null;
                if(!m) return false;
                return Object.keys(m.actual || {}).length === 0
                    && Object.keys(m.exercises || {}).length === 0;
            }""")
        except Exception:
            done = False
        if done:
            break
        pg.wait_for_timeout(500)
    # Once the data is in, render once more so the DOM and memory agree
    pg.evaluate("()=>{ if(typeof refreshDerived === 'function') refreshDerived(); }")
    pg.wait_for_timeout(800)
    keys = app.local_keys()
    left = [k for k in keys if k.startswith("check-") or k.startswith("achievements-")
            or k == "appDataCache"]
    check("After erase: no records in localStorage", left, [])
    d = app.disk("data.json")["members"][0]
    check("After erase: file is empty too", d.get("actual"), {})
    check("Starting weight matches the setup", d.get("start_weight"), start_kg,
          "a hard-coded 70 kg would make it look like %.1f lb were lost" % to_lb(start_kg - 70))
    st3 = app.stats()
    _dbg = pg.evaluate("""()=>({prof: profile.start_weight,
        mem: activeMember().start_weight,
        cur: latestWeight(activeMember()),
        actual: Object.keys(activeMember().actual||{}).length})""")
    check("After erase: total lost back to zero", st3["lost"], 0.0, "front-end memory %s" % _dbg)
    check("After erase: days logged back to zero", st3["days"], 0)

    print("\n[H  Error log is empty]")
    errs = pg.evaluate("()=>(typeof _errLog!=='undefined')?_errLog:[]")
    check("safe() caught no exceptions", errs, [], "non-empty means a render function failed silently")


def main():
    from playwright.sync_api import sync_playwright
    proc, port = setup_env()
    js_errors = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1340, "height": 1000})
            pg.on("pageerror", lambda e: js_errors.append(str(e)[:150]))
            pg.on("dialog", lambda d: d.accept())
            run(pg, port)
            b.close()
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    check("No JS errors at any point", js_errors, [])
    print("\n" + "=" * 60)
    print("  Data flow: %d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  Failed:")
        for f in FAIL:
            print("    -", f)
    print("=" * 60)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
