"""One more pass before sharing -- before handing this to more friends who want to lose
20, 40 or 60 pounds, check again from the angles that are easy to miss.

Each case here maps to a hole found in that review:

  - "Add member" did nothing              -> the member wizard referenced an undefined variable
                                             and failed silently on click
  - the whole household used one body     -> Mom's BMR used the main user's sex, age and height
  - weight typed in the wrong unit        -> 300 typed with the unit on kg went through as 300 kg
                                             (BMI 104) instead of being caught as pounds
  - height typed in meters / feet+inches  -> the server quietly dropped the height, and the
                                             goal-weight floor (BMI 18.5) went with it
  - age 16                                -> an adult calorie deficit for a minor
  - 60 kg person losing 10 kg in 10 weeks -> 1.7% of body weight a week waved through
  - a 210 lb man shown a 1200 cal budget  -> the floor has to depend on sex
  - daylight saving time (US, Europe)     -> a plan week of 6 or 8 days; "week N" off by one
                                             the day after the clocks change
  - iPhone HEIC photos                    -> the browser can't open them, the vision model
                                             can't read them, and nobody said why

Usage:
    python3 test_share.py          run everything
    python3 test_share.py api      only the parts that don't need a browser (seconds)
    python3 test_share.py page     only the browser parts
"""
import datetime
import importlib.util
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
PAGE = "bodycraft.html"
TMP = None
LB = 2.20462
PASS, FAIL = [], []

DATA_FILES = (".server_running.json", "api_config.json", "favorites.json", "food_lookup.json",
              "knowledge.json", "data.json", "content.json", "photos.json", "diary.json",
              "selfies.json", "attach.json", "member_setup.json")


def check(name, got, want, note=""):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    line = "  %-52s %s" % (name, "PASS" if ok else "FAIL")
    if not ok:
        line += "   expected %r, got %r" % (want, got)
    if note:
        line += "   " + str(note)[:300]
    print(line)
    return ok


def check_true(name, cond, note=""):
    return check(name, bool(cond), True, note)


def to_kg(lb):
    """Same as toKg(v, 'lb') on the page: kg rounded to 2 decimals."""
    return round(lb / LB, 2)


def mifflin(sex, kg, cm, age):
    base = 10 * kg + 6.25 * cm - 5 * age
    return max(800, int(round(base + (5 if sex == "male" else -161))))


def fresh_server():
    if os.path.exists(os.path.join(TMP, "app")):
        shutil.rmtree(os.path.join(TMP, "app"), ignore_errors=True)
    app = os.path.join(TMP, "app")
    os.makedirs(app)
    for f in os.listdir(BASE):
        src = os.path.join(BASE, f)
        if os.path.isfile(src) and not f.startswith(".") and f not in DATA_FILES:
            shutil.copy2(src, os.path.join(app, f))
    proc = subprocess.Popen([sys.executable, SERVER], cwd=app,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    flag = os.path.join(app, ".server_running.json")
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


def stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def load_module():
    spec = importlib.util.spec_from_file_location("svc", os.path.join(BASE, SERVER))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def page_js():
    html = open(os.path.join(BASE, PAGE), encoding="utf-8").read()
    return "\n".join(re.findall(r"<script>([\s\S]*?)</script>", html))


def grab(js, name):
    k = js.index("function " + name + "(")
    depth = 0; started = False; e = k
    while e < len(js):
        if js[e] == "{": depth += 1; started = True
        elif js[e] == "}":
            depth -= 1
            if started and depth == 0: break
        e += 1
    return js[k:e + 1]


def node_run(code, env=None):
    node = shutil.which("node")
    if not node:
        return None
    r = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, **(env or {})))
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return json.loads(r.stdout.strip().splitlines()[-1])


# ======================================================================
def part_units():
    js = page_js()
    consts = "\n".join(l for l in js.splitlines() if l.startswith("const SAFE_WEEKLY_LOSS") or l.startswith("const BMI_FLOOR") or l.startswith("const WEIGHT_UNITS") or l.startswith("let unitPref"))
    fns = "\n".join(grab(js, n) for n in (
        "weightUnit", "wUnit", "wShort", "fromKg", "wVal", "fmtW", "fmtWDelta", "fmtNum",
        "safeWeeklyLoss", "cmToFtIn", "ftInToCm", "fmtH", "normalizeHeight", "toKg",
        "checkGoal", "paceW", "fiveTxt", "paceHint"))
    fns = "const store = {get:(k,d)=>globalThis.__unit||d};\n" + fns

    print("\n[Guardrails: big goals, wrong units, minors]")
    out = node_run(consts + "\n" + fns + r"""
const o = [];
const k300 = checkGoal(300, 70, 20, 170, 0, 'kg');
o.push(['300 with the unit on kg is caught as pounds', k300.level === 'block' && /looks like pounds/.test(k300.msg)]);
o.push(['...and the message does the conversion (300 lb is about 136 kg)', /300 lb is about 136 kg/.test(k300.msg)]);
o.push(['300 in lb mode is a normal weight', checkGoal(toKg(300, 'lb'), toKg(250, 'lb'), 52, 178, 40, 'lb').level !== 'block']);
o.push(['180 lb converts to 81.65 kg and back', toKg(180, 'lb') === 81.65 && fromKg(81.65, 'lb') === 180]);
let rt = true;
for(let x = 1000; x <= 4000; x++){ const lb = x / 10; if(fromKg(toKg(lb, 'lb'), 'lb') !== lb){ rt = false; break; } }
o.push(['every 0.1 lb from 100 to 400 survives the kg round trip', rt]);
o.push(['kg passes through unchanged', toKg(78.4, 'kg') === 78.4 && fromKg(78.4, 'kg') === 78.4]);
globalThis.__unit = 'lb';
o.push(['someone who picked lb sees lb', fmtW(10) === '22.0 lb' && fmtWDelta(-10) === '−22.0 lb']);
globalThis.__unit = 'kg';
o.push(['someone who picked kg sees kg', fmtW(10) === '10.0 kg' && fmtWDelta(-10) === '−10.0 kg']);
globalThis.__unit = undefined;
o.push(['no preference yet defaults to lb', weightUnit() === 'lb' && fmtW(10) === '22.0 lb']);
o.push(['5 ft 7 in is 170 cm, 6 ft is 183 cm', ftInToCm(5, 7) === 170 && ftInToCm(6, 0) === 183]);
o.push(['170 cm shows as 5 ft 7 in', cmToFtIn(170).ft === 5 && cmToFtIn(170).inch === 7 && fmtH(170) === "5'7\""]);
globalThis.__unit = 'kg';
o.push(['kg users see height in cm', fmtH(170) === '170 cm']);
globalThis.__unit = undefined;
o.push(['1.7 typed into the cm box becomes 170', normalizeHeight(1.7) === 170 && normalizeHeight(170) === 170 && normalizeHeight('1.65') === 165]);
o.push(['height 1.62 m: goal of 40 kg is blocked', checkGoal(60, 40, 20, 1.62).level === 'block']);
o.push(['BMI floor quoted in lb for lb users (about 107 lb)', /about 107 lb/.test(checkGoal(60, 40, 20, 162, 0, 'lb').msg)]);
o.push(['age 16 is blocked', checkGoal(60, 50, 20, 165, 16).level === 'block' && /18/.test(checkGoal(60, 50, 20, 165, 16).msg)]);
o.push(['age 18 goes through', checkGoal(60, 55, 20, 165, 18).level === 'ok']);
o.push(['weekly cap: 60 kg -> 0.6, 120 kg -> 1, 40 kg -> 0.5', safeWeeklyLoss(60) === 0.6 && safeWeeklyLoss(120) === 1 && safeWeeklyLoss(40) === 0.5]);
o.push(['62 kg losing 10 kg in 10 weeks is slowed to 17 weeks', checkGoal(62, 52, 10, 165).level === 'warn' && checkGoal(62, 52, 10, 165).suggestWeeks === 17]);
o.push(['...and the warning speaks lb to lb users', /Losing 22 lb in 10 weeks/.test(checkGoal(62, 52, 10, 165, 0, 'lb').msg)]);
o.push(['90 kg losing 15 kg in 18 weeks is slowed to 17 weeks', checkGoal(90, 75, 15, 175).suggestWeeks === 17]);
o.push(['90 kg losing 15 kg in 20 weeks goes through', checkGoal(90, 75, 20, 175).level === 'ok']);
const h = paceHint(85, 70, 30, 175, 40, 'lb');
o.push(['big goal: says how long it takes and where the first milestone is', /Losing 33.1 lb/.test(h.text) && /takes at least \d+ weeks/.test(h.text) && /first milestone/.test(h.text)]);
const hk = paceHint(85, 70, 30, 175, 40, 'kg');
o.push(['same hint in kg for kg users', /Losing 15 kg/.test(hk.text) && !/ lb/.test(hk.text.replace(/1–2 lb/g, ''))]);
o.push(['over 65: reminds about strength training', /strength training/.test(paceHint(68, 64, 16, 158, 72).text) && !/strength training/.test(paceHint(68, 64, 16, 158, 40).text)]);
console.log(JSON.stringify(o));
""")
    if out is None:
        print("  (node not installed, skipped)")
    else:
        for label, ok in out:
            check_true(label, ok)

    print("\n[Daylight saving time: dates follow the calendar, not milliseconds]")
    dates = "\n".join(grab(js, n) for n in ("addDays", "dayDiff", "ymd"))
    for tz, label in (("America/New_York", "US Eastern"), ("America/Los_Angeles", "US Pacific"),
                      ("Europe/Berlin", "Germany"), ("Asia/Shanghai", "no DST")):
        out = node_run(dates + r"""
const o = [];
// 2026-11-01 the US leaves DST; 2026-10-25 Europe does; 2026-03-08 the US enters it
o.push(['10-31 plus 7 days is 11-07', ymd(addDays('2026-10-31', 7)) === '2026-11-07']);
o.push(['10-24 plus 7 days is 10-31', ymd(addDays('2026-10-24', 7)) === '2026-10-31']);
o.push(['11-08 minus 10-31 is 8 days', dayDiff('2026-11-08', '2026-10-31') === 8]);
o.push(['03-07 plus 7 days is 03-14 (spring forward)', ymd(addDays('2026-03-07', 7)) === '2026-03-14']);
o.push(['03-29 minus 03-22 is 7 days (Europe springs forward)', dayDiff('2026-03-29', '2026-03-22') === 7]);
o.push(['12:30 AM minus 1 day is still yesterday', ymd(addDays(new Date('2026-11-02T00:30:00'), -1)) === '2026-11-01']);
console.log(JSON.stringify(o));
""", env={"TZ": tz})
        if out is None:
            print("  (node not installed, skipped)"); break
        for l, ok in out:
            check_true(label + ": " + l, ok)
    # The old approach really does break in New York: kept to show why it was changed
    out = node_run(dates + r"""
const d = new Date('2026-10-31T00:00:00');
console.log(JSON.stringify([ymd(new Date(d.getTime() + 7*86400000))]));
""", env={"TZ": "America/New_York"})
    if out is not None:
        check("Adding 7 x 86400000 ms in New York lands on 11-06 (the old bug)", out[0], "2026-11-06")

    print("\n[Server: each member's own body, pounds in the log]")
    m = load_module()
    d = os.path.join(TMP, "svc")
    os.makedirs(d, exist_ok=True)
    m.BASE = d
    for name in ("DATA_FILE", "CONTENT_FILE"):
        if hasattr(m, name):
            setattr(m, name, os.path.join(d, os.path.basename(getattr(m, name))))
    with open(m.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"active": "m1", "members": [
            {"id": "default", "name": "Me", "start_weight": 80, "targets": [], "actual": {}},
            {"id": "m1", "name": "Mom", "start_weight": 68, "targets": [], "actual": {},
             "sex": "female", "age": 58, "height_cm": 158, "activity_level": "light"}]}, f)
    with open(m.CONTENT_FILE, "w", encoding="utf-8") as f:
        json.dump({"profile": {"sex": "male", "age": 36, "height_cm": 178, "activity_level": "sedentary",
                               "start_weight": 80, "weight_unit": "lb"}}, f)
    mid, mem, prof = m._member_and_profile("m1")
    check("Member's sex overrides the main profile", prof.get("sex"), "female")
    check("Member's age overrides the main profile", prof.get("age"), 58)
    check("Member's height overrides the main profile", prof.get("height_cm"), 158)
    check("Member's activity overrides the main profile", prof.get("activity_level"), "light")
    check("Member's starting weight overrides the main profile", prof.get("start_weight"), 68)
    mid, mem, prof = m._member_and_profile("default")
    check("Main member still uses the main profile", prof.get("sex"), "male")
    got = m._sanitize_daily_log({"weight": 75}, ["lunch"])
    check("A kg weight is accepted as is", got.get("weight"), 75)
    got = m._sanitize_daily_log({"body_kg": 182}, ["lunch"], text="weighed in at 182 lb this morning")
    check("Pounds copied through unconverted get converted", got.get("weight"), to_kg(182),
          "182 lb is %.2f kg" % to_kg(182))
    got = m._sanitize_daily_log({"body_kg": 82.5}, ["lunch"], text="weighed in at 182 lb this morning")
    check("An already-converted weight is left alone", got.get("weight"), 82.5)
    got = m._sanitize_daily_log({"body_kg": 82}, ["lunch"], text="82 kg on the scale, about 180 lb")
    check("Text that mentions kg isn't converted again", got.get("weight"), 82)
    src = open(os.path.join(BASE, SERVER), encoding="utf-8").read().splitlines()
    rule = "".join(l for l in src if '\\"180 lb\\"' in l or '"180 lb"' in l)
    check_true("Log prompt tells the model to convert pounds to kg",
               "kg" in rule and "2.2046" in rule, rule.strip()[:120])


# ======================================================================
def part_page():
    from playwright.sync_api import sync_playwright
    print("\n[Page: wizard in lb with ft/in, kg switch, lb-looking kg, under 18]")
    proc, port = fresh_server()
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(locale="en-US", timezone_id="America/New_York")
            pg = ctx.new_page()
            # Pinned to 2026-11-02 1:00 PM, the day after the US leaves daylight saving time
            pg.clock.install(time=datetime.datetime(2026, 11, 2, 13, 0))
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_timeout(2600)
            check("Wizard opens in lb", pg.input_value("#s0Unit"), "lb")
            check_true("lb mode asks for feet and inches", pg.is_visible("#s0Ft") and not pg.is_visible("#s0Height"))
            pg.fill("#s0Name", "Bob"); pg.select_option("#s0Sex", "male")
            pg.fill("#s0Age", "16"); pg.fill("#s0Ft", "5"); pg.fill("#s0In", "9")
            pg.fill("#s0Start", "210"); pg.fill("#s0Target", "176"); pg.fill("#s0Weeks", "20")
            pg.wait_for_timeout(200)
            hint = pg.locator("#s0Hint").inner_text()
            check_true("Age 16: the hint says under 18", "under 18" in hint, hint[:80])
            pg.click("#s0Ok"); pg.wait_for_timeout(500)
            check("Age 16: Get started doesn't go through", pg.locator("#s0Ok").count(), 1)
            pg.fill("#s0Age", "46"); pg.wait_for_timeout(200)
            hint = pg.locator("#s0Hint").inner_text()
            check_true("210 lb -> 176 lb: hint talks in lb (34 lb)", "Losing 34 lb" in hint and " kg" not in hint, hint[:160])
            check_true("Big goal: says how many weeks, and the first milestone", "takes at least" in hint and "first milestone" in hint, hint[:200])
            check("5 ft 9 in fills the hidden cm field with 175", pg.input_value("#s0Height"), "175")

            pg.select_option("#s0Unit", "kg"); pg.wait_for_timeout(200)
            check("Switch to kg: 210 lb shows as 95.3 kg", pg.input_value("#s0Start"), "95.3")
            check("Switch to kg: 176 lb shows as 79.8 kg", pg.input_value("#s0Target"), "79.8")
            check_true("Switch to kg: height shows in cm", pg.is_visible("#s0Height") and not pg.is_visible("#s0Ft"))
            check("Switch to kg: height is 175 cm", pg.input_value("#s0Height"), "175")
            hint = pg.locator("#s0Hint").inner_text()
            check_true("Hint now in kg", re.search(r"Losing 15\.\d kg", hint) and " lb" not in hint, hint[:120])
            # Toggling the unit back without touching the numbers must give back exactly what was typed
            pg.select_option("#s0Unit", "lb"); pg.wait_for_timeout(200)
            check("lb -> kg -> lb: current weight is still 210", pg.input_value("#s0Start"), "210")
            check("lb -> kg -> lb: goal weight is still 176", pg.input_value("#s0Target"), "176")
            check("lb -> kg -> lb: height still 5 ft 9 in", [pg.input_value("#s0Ft"), pg.input_value("#s0In")], ["5", "9"])
            pg.select_option("#s0Unit", "kg"); pg.wait_for_timeout(200)
            pg.fill("#s0Start", "210"); pg.wait_for_timeout(200)
            hint = pg.locator("#s0Hint").inner_text()
            check_true("210 typed with kg selected: asks if that's pounds", "Is 210 kg right?" in hint and "switch the unit to lb" in hint, hint[:120])
            pg.fill("#s0Start", "300"); pg.wait_for_timeout(200)
            hint = pg.locator("#s0Hint").inner_text()
            check_true("300 typed with kg selected: blocked as pounds", "looks like pounds" in hint and "136 kg" in hint, hint[:160])
            pg.click("#s0Ok"); pg.wait_for_timeout(500)
            check("300 kg doesn't go through", pg.locator("#s0Ok").count(), 1)
            pg.fill("#s0Start", "95"); pg.wait_for_timeout(100)
            pg.select_option("#s0Unit", "lb"); pg.wait_for_timeout(200)
            check("Back to lb: 95 kg converts to 209.4 lb", pg.input_value("#s0Start"), "209.4")
            check_true("Back to lb: height shows ft/in again", pg.is_visible("#s0Ft") and not pg.is_visible("#s0Height"))
            # Type the lb values again so what follows doesn't depend on the unit round trip above
            pg.fill("#s0Start", "210"); pg.fill("#s0Target", "176"); pg.wait_for_timeout(200)
            pg.click("#s0Ok"); pg.wait_for_timeout(2600)
            check("Wizard closed", pg.locator("#s0Ok").count(), 0)
            prof = pg.evaluate("({h: profile.height_cm, sw: profile.start_weight, w: weeks.length, u: profile.weight_unit})")
            check("5 ft 9 in stored as 175 cm", prof["h"], 175)
            check("Starting weight stored in kg", prof["sw"], to_kg(210))
            check("Gentle pace keeps the 20 weeks", prof["w"], 20)
            check("Goal weight stored at the precision typed (176 lb in kg)",
                  pg.evaluate("activeMember().targets.slice(-1)[0]"), to_kg(176),
                  "0.1 kg rounding turns a 176 lb goal into 175.9 lb")
            check("Unit preference saved in the profile", prof["u"], "lb")
            check("Unit preference remembered in the browser", pg.evaluate("store.get('weightUnit')"), "lb")
            loss = pg.locator("#statLoss").inner_text()
            check("Planned loss shown in lb", loss, "−34.0 lb")
            check("Goal weight shown in lb", pg.locator("#statWeight").inner_text(), "210→176")

            print("\n[Page: budget floor for men is 1500]")
            pg.evaluate("""()=>{ profile.target_kcal = null; const m = activeMember(); m.target_weight = 80; m.weeks = 8; renderBudget(); }""")
            check("Budget doesn't go below 1500", pg.locator("#statKcal").inner_text(), "1500")
            pg.evaluate("""()=>{ profile.sex = 'female'; renderBudget(); }""")
            check("Floor for women is 1200", pg.locator("#statKcal").inner_text(), "1200")
            pg.evaluate("""()=>{ profile.sex = 'male'; const m = activeMember(); delete m.target_weight; delete m.weeks; renderBudget(); }""")

            print("\n[Page: the DST week still has 7 days]")
            pg.evaluate("""()=>{ profile.start_date = '2026-10-19'; renderWeekTable(); }""")
            rows = pg.evaluate("[...document.querySelectorAll('#weekTable tr')].slice(0,4).map(r => r.children[1].firstChild.textContent.trim())")
            check("Week 3 (across the 11-01 change) is Nov 2 - Nov 8", rows[2], "Nov 2 – Nov 8")
            check("Week 2 is Oct 26 - Nov 1", rows[1], "Oct 26 – Nov 1")
            check("The day after the change is week 3", pg.evaluate("weekOfPlan()"), 3)
            check("By the calendar, 11-02 is 14 days after 10-19", pg.evaluate("dayDiff('2026-11-02', '2026-10-19')"), 14)
            pg.evaluate("""()=>{ profile.start_date = todayKey(); }""")

            print("\n[Page: logging weight in lb and kg]")
            pg.click('#tabs button[data-t="weight"]'); pg.wait_for_timeout(500)
            check("Unit button starts on lb", pg.locator("#wUnitBtn").inner_text(), "lb")
            pg.fill("#wInput", "40"); pg.click("#wSave"); pg.wait_for_timeout(400)
            check_true("40 lb is rejected as unrealistic, range in lb", "realistic weight" in pg.locator("#wHint").inner_text()
                       and "lb)" in pg.locator("#wHint").inner_text(), pg.locator("#wHint").inner_text())
            pg.click("#wUnitBtn"); pg.wait_for_timeout(100)
            check("Tap once: kg", pg.locator("#wUnitBtn").inner_text(), "kg")
            pg.fill("#wInput", "209"); pg.click("#wSave"); pg.wait_for_timeout(400)
            check_true("209 with kg selected is questioned as pounds", "looks like pounds" in pg.locator("#wHint").inner_text(), pg.locator("#wHint").inner_text())
            check("...and nothing is saved", pg.evaluate("activeMember().actual[todayKey()]"), None)
            pg.fill("#wInput", "94.5"); pg.click("#wSave"); pg.wait_for_timeout(800)
            check("94.5 kg saved as 94.5", pg.evaluate("activeMember().actual[todayKey()]"), 94.5)
            check_true("Lost label in kg", "kg" in pg.locator("#statLostLabel").inner_text(), pg.locator("#statLostLabel").inner_text())
            pg.click("#wUnitBtn"); pg.wait_for_timeout(100)
            check("Tap again: back to lb", pg.locator("#wUnitBtn").inner_text(), "lb")
            pg.fill("#wInput", "207"); pg.click("#wSave"); pg.wait_for_timeout(800)
            check("207 lb saved as %.2f kg" % to_kg(207), pg.evaluate("activeMember().actual[todayKey()]"), to_kg(207))
            check_true("Lost label in lb", "lb" in pg.locator("#statLostLabel").inner_text(), pg.locator("#statLostLabel").inner_text())
            check("Lost card shows 3.0 lb (210 - 207)", pg.locator("#statWater").inner_text(), "3.0")
            check("Unit choice saved to the profile", pg.evaluate("profile.weight_unit"), "lb")

            print("\n[Page: add a member, computed from her own body]")
            pg.click("#addMemberBtn"); pg.wait_for_timeout(400)
            check("Member wizard opens in the current unit", pg.input_value("#wUnit"), "lb")
            pg.fill("#wName", "Mom"); pg.select_option("#wSex", "female"); pg.fill("#wAge", "58")
            pg.fill("#wHeightFt", "5"); pg.fill("#wHeightIn", "2"); pg.select_option("#wAct", "light")
            pg.fill("#wStart", "150"); pg.fill("#wTarget", "137"); pg.fill("#wWeeks", "16")
            pg.click("#wizOk"); pg.wait_for_timeout(1500)
            check("Member added", pg.evaluate("appData.members.length"), 2)
            check("Switched to the new member", pg.evaluate("activeMember().name"), "Mom")
            check("Member has her own sex, age, height, activity", pg.evaluate("[activeMember().sex, activeMember().age, activeMember().height_cm, activeMember().activity_level]"), ["female", 58, 157, "light"])
            check("Member weights stored in kg", pg.evaluate("[activeMember().start_weight, activeMember().target_weight]"), [to_kg(150), to_kg(137)])
            bmr = pg.evaluate("calcBMR(%s)" % to_kg(150))
            check("Mom's BMR uses her own body", bmr["value"], mifflin("female", to_kg(150), 157, 58), bmr["note"])
            check_true("BMR note in her units (5'2\", 150.0 lb)", "5'2\"" in bmr["note"] and "150.0 lb" in bmr["note"], bmr["note"])
            check("Activity factor is her own", pg.evaluate("activityInfo().k"), "light")
            check_true("No JS errors (Add member used to throw ReferenceError)", not errs, errs[:2])
            pg.evaluate("switchMember('default')"); pg.wait_for_timeout(600)
            note = pg.evaluate("calcBMR(%s)" % to_kg(210))["note"]
            check_true("Back on the main member, BMR uses the main profile", note.startswith("male, 46 years, 5'9\""), note)

            print("\n[Page: HEIC photos explained, big images shrunk]")
            got = pg.evaluate("""async ()=>{
                const f1 = new File([new Uint8Array(10)], 'IMG_0001.HEIC', {type: 'image/heic'});
                let m1 = ''; try{ await readImageForUpload(f1); }catch(e){ m1 = e.message; }
                const f0 = new File([new Uint8Array(10)], 'IMG_0002.heic', {type: ''});
                let m0 = ''; try{ await readImageForUpload(f0); }catch(e){ m0 = e.message; }
                const c = document.createElement('canvas'); c.width = 3200; c.height = 2400;
                c.getContext('2d').fillStyle = '#c96'; c.getContext('2d').fillRect(0,0,3200,2400);
                const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.9));
                const f2 = new File([blob], 'big.jpg', {type: 'image/jpeg'});
                const im = await readImageForUpload(f2);
                const img = new Image(); img.src = im.dataUrl; await img.decode();
                return {m1, m0, w: img.naturalWidth, ext: im.ext};
            }""")
            check_true("HEIC gets an actionable message", "Most Compatible" in got["m1"] and "JPG" in got["m1"], got["m1"][:80])
            check_true("HEIC caught by file name when the type is empty", "Most Compatible" in got["m0"], got["m0"][:80])
            check("A 3200-wide image shrinks to 1600", got["w"], 1600)
            check("Shrunk image is a jpg", got["ext"], "jpg")

            print("\n[Page: plan ends, next plan starts from where you are]")
            pg.evaluate("""(w)=>{ const m = activeMember(); m.actual[todayKey()] = w; profile.end_date = '2026-10-30'; renderPlanEnd(); }""", to_kg(199))
            txt = pg.locator("#planEndBar").inner_text()
            check_true("Ended short of goal: says how much is lost and how much is left (lb)", "down 11.0 lb" in txt and "23.0 lb to go" in txt, txt[:160])
            pg.evaluate("""(w)=>{ const m = activeMember(); m.actual[todayKey()] = w; renderPlanEnd(); }""", to_kg(175.5))
            txt = pg.locator("#planEndBar").inner_text()
            check_true("Ended at goal: says maintenance comes next", "reached your 176.0 lb goal" in txt and "maintenance" in txt, txt[:160])
            pg.evaluate("""()=>{ profile.end_date = '2027-06-01'; renderPlanEnd(); }""")
            check("Not ended yet: nothing shown", pg.locator("#planEndBar").inner_text(), "")
            pg.click("#planEndGo") if pg.locator("#planEndGo").count() else pg.evaluate("reopenSetup()")
            pg.wait_for_timeout(600)
            check("Setup again keeps the name", pg.input_value("#s0Name"), "Bob")
            check("Setup again keeps the height (ft/in)", [pg.input_value("#s0Ft"), pg.input_value("#s0In")], ["5", "9"])
            check("Setup again keeps the unit", pg.input_value("#s0Unit"), "lb")
            check("Current weight is the latest weigh-in, in lb", pg.input_value("#s0Start"), "175.5")
            check("Goal left empty when already reached", pg.input_value("#s0Target"), "")
            pg.fill("#s0Target", "160"); pg.wait_for_timeout(200)
            pg.click("#s0Ok"); pg.wait_for_timeout(2600)
            check("New plan starts at 175.5 lb", pg.evaluate("activeMember().start_weight"), to_kg(175.5))
            check("Old weigh-ins are all still there", pg.evaluate("Object.keys(activeMember().actual).length") >= 1, True)
            check_true("Every week's tips are filled in (past week 18 too)", pg.evaluate("weeks.every(w => w.ex && w.dt)"))

            print("\n[Page: skipping the wizard shows no fake numbers]")
            # The skipped-wizard state: the member has no targets, and the weeks in content are
            # only the factory template (no w)
            pg.evaluate("""()=>{ const m = activeMember(); m.targets = []; m.target_weight = 0; m.actual = {}; weeks.forEach(w => { delete w.w; }); renderWeightAll(); refreshDerived(); }""")
            check("No plan: goal weight shows ?→?", pg.locator("#statWeight").inner_text(), "?→?")
            check("No plan: budget says add weight and goal first", pg.locator("#statKcalLabel").inner_text(), "Add your weight and goal first")
            check("No plan, no weigh-ins: latest weight is empty, not a default", pg.evaluate("latestWeight(activeMember())"), None)

            print("\n[Page: text size]")
            pg.click("#fontBtn"); pg.wait_for_timeout(200)
            check("One tap makes it larger", pg.evaluate("document.documentElement.style.zoom"), "1.15")
            pg.reload(); pg.wait_for_timeout(2600)
            check("Still large after reload", pg.evaluate("document.documentElement.style.zoom"), "1.15")
            pg.click("#fontBtn"); pg.click("#fontBtn"); pg.wait_for_timeout(200)
            check("Three taps total: back to normal", pg.evaluate("document.documentElement.style.zoom"), "")
            check("No page errors", errs, [])
            b.close()
    finally:
        stop(proc)


def main():
    global TMP
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg not in ("", "api", "page"):
        print("usage: python3 test_share.py [api|page]")
        return 2
    TMP = tempfile.mkdtemp(prefix="bodycraft_share_")
    try:
        if arg in ("", "api"):
            part_units()
        if arg in ("", "page"):
            part_page()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 64)
    print("  Pre-share check: %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  x " + f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
