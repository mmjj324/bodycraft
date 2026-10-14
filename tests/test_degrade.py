"""Degradation and guard-rail tests -- does it work without AI, do the guards hold.

Two layers in one, because both ask "what happens in the worst case":

Degradation: no API key, server not up, the model returns garbage. This is the first
             experience for most new users, and for a long time it wasn't tested at all.
             Features should fall back to something usable, not a blank screen or a spinner.

Guards:      mistakes here can actually hurt someone. Losing too fast, a goal weight that's
             too low, badges that reward the scale dropping, reviews that stoke anxiety.
             Run this before changing any AI prompt.

Usage: python3 tests/test_degrade.py
"""
import importlib.util
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = BASE
SERVER = "bodycraft_server.py"
LB = 2.20462

# Personal data and runtime files are never copied into a test copy
PRIVATE = {".server_running.json", ".widget_running.json", ".rollback.json", ".llm_debug.txt",
           "api_config.json", "api_key.txt", "data.json", "content.json", "diary.json",
           "photos.json", "attach.json", "selfies.json", "knowledge.json", "food_lookup.json",
           "favorites.json", "member_setup.json"}

PASS, FAIL = [], []


def check(name, got, want, note=""):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    line = "  %-52s %s" % (name, "PASS" if ok else "FAIL")
    if not ok:
        line += "   expected %r, got %r" % (want, got)
    if note:
        line += "   " + note
    print(line)
    return ok


def check_true(name, cond, note=""):
    return check(name, bool(cond), True, note)


def copy_app(dst):
    """Copy the app's top-level files (no folders, no personal data) into dst."""
    for f in os.listdir(SRC):
        p = os.path.join(SRC, f)
        if os.path.isfile(p) and f not in PRIVATE and not f.endswith((".pyc", ".tmp")):
            shutil.copy(p, os.path.join(dst, f))


def load_module():
    """Import the server directly; pure functions don't need it running."""
    d = tempfile.mkdtemp(prefix="bodycraft_degrade_mod_")
    shutil.copy(os.path.join(SRC, SERVER), os.path.join(d, "svc.py"))
    cwd = os.getcwd()
    os.chdir(d)
    spec = importlib.util.spec_from_file_location("svc", os.path.join(d, "svc.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.BASE = d
    os.chdir(cwd)
    return m, d


def fake_ai(m):
    """Make the module believe AI is set up, without a real provider."""
    m.load_llm_config = lambda: {"api_key": "k", "base_url": "https://x", "model": "m"}
    m.ai_configured = lambda: "k"


def test_guardrails(m):
    print("\n[Guard - pace of loss]")
    lvl, msg, sug = m.check_goal(78, 72, 16, 170)
    check("reasonable goal passes", lvl, "ok", "172 -> 159 lb, 16 weeks")
    lvl, msg, sug = m.check_goal(78, 60, 4, 170)
    check_true("40 lb in four weeks is stopped", lvl in ("block", "warn"), "level=%s" % lvl)
    lvl, msg, sug = m.check_goal(78, 70, 4, 170)
    check_true("4+ lb a week gets a slower suggestion", lvl == "warn" and (sug or 0) > 4,
               "suggests %s weeks" % sug)
    check_true("pace warning is in pounds", "lb a week" in (msg or "") and "kg a week" not in (msg or ""),
               (msg or "")[:60])

    print("\n[Guard - weight floor]")
    lvl, msg, _ = m.check_goal(60, 45, 20, 170)
    check("goal below BMI floor is refused", lvl, "block", "99 lb at 5'7\" = BMI 15.6")
    check_true("refusal names the goal in pounds", "99 lb" in (msg or ""), (msg or "")[:50])
    lvl, _, _ = m.check_goal(60, 55, 20, 170)
    check_true("BMI above 18.5 passes", lvl != "block", "121 lb at 5'7\" = BMI 19.0")
    lvl, _, _ = m.check_goal(60, 35, 20, None)
    check_true("absolute floor still applies without height", lvl in ("block", "warn"), "level=%s" % lvl)

    print("\n[Guard - gaining and maintaining are not limited]")
    lvl, _, _ = m.check_goal(60, 65, 8, 170)
    check("gaining isn't stopped by the loss guard", lvl, "ok")
    lvl, _, _ = m.check_goal(60, 60, 8, 170)
    check("maintaining isn't stopped", lvl, "ok")

    print("\n[Guard - odd input doesn't raise]")
    for args in [(0, 0, 0, 0), (-5, -5, 1, 170), ("x", "y", "z", "w"),
                 (None, None, None, None), (99999, 1, 1, 1)]:
        try:
            r = m.check_goal(*args)
            ok = isinstance(r, tuple) and len(r) == 3 and r[0] in ("ok", "warn", "block")
        except Exception as e:
            ok = False
            r = str(e)[:40]
        check_true("odd input %s doesn't raise" % (args[:2],), ok)

    print("\n[Guard - safety clause added once, at the single exit]")
    # Not scanning each function's source any more. The rule lives in _llm_chat, the single
    # exit, so it's checked by intercepting the messages actually sent: whichever path, whether
    # the prompt carried it or not, text or image, the clause is there at the exit, exactly once.
    sent = []
    fake_ai(m)
    real_open = m.urllib.request.urlopen

    class _Resp:
        def read(self):
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def fake_open(req, timeout=0):
        body = m.json.loads(req.data.decode("utf-8"))
        sent.append(body["messages"])
        return _Resp()
    m.urllib.request.urlopen = fake_open
    try:
        m.meal_ideas("lunch", "", {})                                             # carries it itself
        m.kcal_for_foods([{"name": "White rice", "weight": 100, "unit": "g"}])    # doesn't
        m.exercise_kcal("Walk", 30, "moderate", 70)                               # doesn't
        m.food_lookup("Egg", "each", 1)                                           # doesn't
        m.coach_reply("warm", [], {}, None)                                       # via the persona style
    finally:
        m.urllib.request.urlopen = real_open
    check_true("at least five model calls intercepted", len(sent) >= 5, "intercepted %d" % len(sent))
    key = m.SAFETY_CLAUSE.strip()[:20]
    for i, msgs in enumerate(sent):
        texts = []
        for x in msgs:
            c = x.get("content")
            texts.append(c if isinstance(c, str) else " ".join(
                str(y.get("text", "")) for y in (c or []) if isinstance(y, dict)))
        joined = "\n".join(texts)
        check_true("call %d carries the safety clause" % (i + 1), key in joined)
        check("call %d carries it only once" % (i + 1), joined.count(key), 1)

    print("\n[Guard - badges reward showing up, not the scale]")
    # Fast drop but barely logged: shouldn't earn a pile of badges
    fast = m.period_badges({
        "days": [{"date": "2026-09-01", "weight": 78},
                 {"date": "2026-09-03", "weight": 72}],
        "total_days": 14, "weight_start": 78, "weight_end": 72})
    # Slow but logged every day: should earn more
    steady = m.period_badges({
        "days": [{"date": "2026-09-%02d" % d, "weight": 78 - d * 0.05,
                  "diary": "pretty good day", "exercises": [{"duration": 30}]}
                 for d in range(1, 15)],
        "total_days": 14, "weight_start": 78, "weight_end": 77.3})
    check_true("steady logging earns more badges", len(steady) > len(fast),
               "steady %d vs fast drop %d" % (len(steady), len(fast)))
    names = " ".join(b.get("name", "") for b in steady)
    check_true("badge wording isn't about weight numbers",
               not re.search(r"\b(lb|lbs|pounds?|kg|kilos?)\b", names, re.I),
               names[:40])
    # Stopping and coming back deserves encouragement
    back = m.period_badges({
        "days": [{"date": "2026-09-01"}, {"date": "2026-09-02"},
                 {"date": "2026-09-10"}, {"date": "2026-09-11"}],
        "total_days": 14})
    check_true("coming back after a gap earns a badge", any(b.get("name") == "Came back" for b in back),
               "%d badge(s)" % len(back))


def test_bmr(m):
    """BMR is calculated on the page (calcBMR). The JS is pulled out of the HTML and run with
    Node -- the formula's fallback paths (missing fields, absurd values) deserve their own test,
    and moving to the front end shouldn't mean dropping it. The server keeps a copy of the
    formula (_bmr_value) for summaries; the two are checked against each other.
    No Node installed: skipped, not failed."""
    print("\n[Degrade - BMR with missing fields (page calcBMR)]")
    node = shutil.which("node")
    if not node:
        print("  (node not installed, skipped)")
        return
    html = open(os.path.join(SRC, "bodycraft.html"), encoding="utf-8").read()
    js = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", html))

    def grab(name):
        k = js.index("function " + name + "(")
        depth = 0; started = False; e = k
        while e < len(js):
            if js[e] == "{": depth += 1; started = True
            elif js[e] == "}":
                depth -= 1
                if started and depth == 0: break
            e += 1
        return js[k:e + 1]
    # calcBMR reads the body through bodyOf() (a member's own first, then the main profile),
    # and its note shows weight and height in the user's units.
    helpers = ["weightUnit", "wShort", "fromKg", "wVal", "fmtW", "normalizeHeight", "cmToFtIn", "fmtH",
               "bodyOf", "calcBMR"]
    fn = ("const WEIGHT_UNITS = {lb: {name: 'lb', short: 'lb', f: 2.20462}, kg: {name: 'kg', short: 'kg', f: 1}};\n"
          "let unitPref = null;\nconst store = {get: (k, d) => d};\n"
          + "\n".join(grab(h) for h in helpers)
          + "\nfunction activeMember(){ return globalThis.member || {id:'default'}; }\n")
    harness = fn + """
const cases = [
  [{sex:'male',age:25,height_cm:175}, 70, 'mifflin', 1674, "25-year-old man, 5'9\\", 154 lb"],
  [{sex:'female',age:55,height_cm:163}, 70, 'mifflin', 1283, "55-year-old woman, 5'4\\", 154 lb"],
  [{}, 70, 'perkg', 1540, 'nothing filled in -> per kg, 70 x 22'],
  [{sex:'male',height_cm:175}, 70, 'perkg', 1540, 'no age -> per kg'],
  [{sex:'male',age:999,height_cm:175}, 70, 'perkg', 1540, 'absurd age -> per kg'],
  [{sex:'male',age:25,height_cm:9999}, 70, 'perkg', 1540, 'absurd height -> per kg'],
  [{sex:'female',age:38,height_cm:170,bmr_per_kg:25}, 78, 'mifflin', 1492, 'all three known -> per_kg ignored'],
];
const out = [];
for (const [prof, w, how, val, label] of cases) {
  globalThis.profile = prof;
  const r = calcBMR(w);
  out.push({label, ok: r.how === how && r.value === val, got: r.value + '/' + r.how, prof, w, value: r.value});
}
// A family member: their own body, not the main profile
globalThis.profile = {sex:'male',age:25,height_cm:175};
globalThis.member = {id:'m1', sex:'female', age:55, height_cm:163};
{ const r = calcBMR(70); out.push({label:"family member uses their own sex, age and height", ok: r.how === 'mifflin' && r.value === 1283, got: r.value + '/' + r.how}); }
globalThis.member = {id:'m1'};
{ const r = calcBMR(70); out.push({label:'member without body info falls back to main profile', ok: r.how === 'mifflin' && r.value === 1674, got: r.value + '/' + r.how}); }
// The note speaks the user's units: pounds and feet by default, kg and cm when chosen
globalThis.member = null;
globalThis.profile = {sex:'male',age:25,height_cm:175};
{ const n = calcBMR(70).note; out.push({label:'note in lb and ft/in by default', ok: n.includes('154.3 lb') && n.includes("5'9\\""), got: n}); }
globalThis.profile = {sex:'male',age:25,height_cm:175,weight_unit:'kg'};
{ const n = calcBMR(70).note; out.push({label:'note in kg and cm when kg is chosen', ok: n.includes('70.0 kg') && n.includes('175 cm'), got: n}); }
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        check_true("calcBMR runs", False, (r.stderr or "")[:120])
        return
    rows = json.loads(r.stdout.strip().splitlines()[-1])
    for row in rows:
        check_true(row["label"], row["ok"], "got " + row["got"])
    # same formula on both sides
    same = [(row["label"], row["value"], m._bmr_value(row["w"], row["prof"]))
            for row in rows if "prof" in row]
    diff = [x for x in same if x[1] != x[2]]
    check_true("server _bmr_value matches page calcBMR", not diff, str(diff[:2]) if diff else "%d cases" % len(same))


def test_lookup_match(m):
    """My Foods fuzzy matching: match what should match, leave alone what shouldn't.
    Matching "chicken parmesan" to "Chicken breast" gives a number that's half wrong --
    worse than asking the AI."""
    print("\n[Structure - My Foods fuzzy matching]")
    tb = {m.lookup_key(row[0], row[1]): {"per": row[2], "name": row[0], "unit": row[1], "note": ""}
          for row in m.FOOD_SEED}
    cases = [
        ("Egg", "g", True, "Egg|each", "unit never touched: take the list's \"each\""),
        ("Egg", "g", False, None, "user picked grams on purpose: don't force it"),
        ("egg", "each", False, "Egg|each", "case doesn't matter"),
        ("Grilled chicken breast", "g", True, "Chicken breast|g", "cooking word dropped"),
        ("Hard-boiled egg", "g", True, "Egg|each", "hyphenated cooking word"),
        ("Ground beef", "g", True, "Ground beef 90% lean|g", "short form"),
        ("Chicken parmesan", "g", True, None, "\"parmesan\" isn't a cooking word: ask the AI instead"),
        ("Salmon sashimi", "g", True, None, "\"sashimi\" isn't a cooking word"),
        ("Steamed broccoli", "g", True, "Broccoli|g", ""),
        ("Never seen this", "g", True, None, ""),
    ]
    for name, unit, auto, want, note in cases:
        rec, tu = m.lookup_match(tb, name, unit, auto)
        got = ("%s|%s" % (rec["name"], tu)) if rec else None
        check("%s -> %s" % (name, want or "no match"), got, want, note)


def test_coach_context(m):
    """The coach must get the whole day: snacks, meals counted at their default, several workouts.
    Once the meals were hard-coded to three (snacks never got in); once exercise switched to a
    list but was read as an object (exercise never got in)."""
    print("\n[Structure - coach context complete]")
    ctx = {
        "weights": {"2026-09-10": 76.4},
        "meals": {"breakfast": {"name": "Breakfast", "cal": 132, "source": "recorded",
                                "items": "hard-boiled eggs, black coffee"},
                  "lunch": {"name": "Lunch", "cal": 550, "source": "default", "items": ""},
                  "snack": {"name": "Snack", "cal": 100, "source": "default", "items": ""},
                  "late": {"name": "Late snack", "cal": 200, "source": "recorded", "items": "Greek yogurt"}},
        "exercise": [{"type": "Walk", "duration": 40, "kcal": 160, "intensity": "moderate"},
                     {"type": "Run", "duration": 25, "kcal": 280}],
        "sleep": {"bed": "23:30", "wake": "06:50", "quality": 4}, "diary": "",
    }
    sent = []
    fake_ai(m)
    real_open = m.urllib.request.urlopen

    class _R:
        def read(self): return b'{"choices":[{"message":{"content":"ok"}}]}'

    def fake(req, timeout=0):
        sent.append(m.json.loads(req.data.decode("utf-8"))["messages"]); return _R()
    m.urllib.request.urlopen = fake
    try:
        m.coach_reply("warm", [], ctx, [{"role": "user", "content": "how did today go?"}])
    finally:
        m.urllib.request.urlopen = real_open
    txt = "\n".join(x["content"] if isinstance(x["content"], str) else "" for x in sent[-1])
    for k, label in [("Snack 100 kcal", "snack made it into the prompt"),
                     ("Late snack 200 kcal", "a custom meal makes it in too"),
                     ("(default estimate)", "meals at their default are marked"),
                     ("hard-boiled eggs", "logged food detail made it in"),
                     ("Total 982 kcal", "total is right"),
                     ("Run 25 min", "second workout made it in"),
                     ("65 minutes total", "workout minutes summed"),
                     ("lb (76.4 kg)", "weigh-in shown in pounds with kg alongside")]:
        check_true(label, k in txt)


def test_llm_garbage(m):
    print("\n[Degrade - the model returns garbage]")
    fake_ai(m)
    cases = [
        ("empty string", ""),
        ("not JSON", "Sorry, I can't help with that"),
        ("JSON with the wrong shape", '{"foo":"bar"}'),
        ("ideas isn't a list", '{"ideas":"scrambled eggs"}'),
        ("list mixed with strings", '{"ideas":["scrambled eggs",{"title":"x","cal":300}]}'),
        ("negative calories", '{"ideas":[{"title":"x","cal":-500}]}'),
        ("absurd calories", '{"ideas":[{"title":"x","cal":999999}]}'),
        ("fields missing", '{"ideas":[{}]}'),
    ]
    for label, resp in cases:
        m._call_llm_text = lambda p, _r=resp, **k: _r
        try:
            r = m.meal_ideas("lunch", "", {})
            ok = isinstance(r, dict) and "ok" in r
            if r.get("ok"):
                for it in r.get("ideas", []):
                    if not (0 <= it.get("cal", 0) <= 5000):
                        ok = False
        except Exception as e:
            ok = False
            label += " (raised %s)" % str(e)[:30]
        check_true("meal ideas - %s" % label, ok)

    for label, resp in cases[:5]:
        m._call_llm_text = lambda p, _r=resp, **k: _r
        try:
            r = m.kcal_for_foods([{"name": "Chicken tikka masala", "weight": 150, "unit": "g"}])
            ok = isinstance(r, dict) and "ok" in r
        except Exception as e:
            ok = False
            label += " (raised %s)" % str(e)[:30]
        check_true("calorie lookup - %s" % label, ok)


def test_no_key(port):
    print("\n[Degrade - no API key]")
    B = "http://127.0.0.1:%d" % port

    def req(path, payload=None):
        h = {"Content-Type": "application/json", "Origin": B}
        d = json.dumps(payload).encode() if payload is not None else None
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(B + path, data=d, headers=h), timeout=10)
            return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:
            return 0, {"err": str(e)[:40]}

    ai_endpoints = [
        ("/api/meal_ideas", {"slot": "lunch"}),
        ("/api/food_advice", {"question": "what should I eat?"}),
        ("/api/kcal_exercise", {"type": "Walk", "duration": 30}),
        ("/api/daily_log", {"text": "walked 30 minutes", "meals": [],
                            "habits": [], "exercise_types": ["Walk", "Other"]}),
        ("/api/coach", {"persona": "warm", "events": [], "context": {}}),
        ("/api/fetch_recipe", {"url": "https://example.com/r"}),
    ]
    for path, payload in ai_endpoints:
        code, d = req(path, payload)
        ok = code == 200 and isinstance(d, dict) and d.get("ok") is False and d.get("err")
        check_true("%s says so instead of spinning" % path.split("/")[-1], ok,
                   (d or {}).get("err", "")[:30])
    code, d = req("/api/coach", {"persona": "roast", "events": [], "context": {}})
    check_true("coach without a key asks for setup", code == 200 and (d or {}).get("need_setup") is True,
               "need_setup=%r" % (d or {}).get("need_setup"))

    print("\n[Degrade - features that don't need AI still work]")
    offline = [
        ("/api/data", None), ("/api/content", None),
        ("/api/check_goal", {"start_weight": 78, "target": 72,
                             "weeks": 16, "height_cm": 170}),
    ]
    for path, payload in offline:
        code, _ = req(path, payload)
        check("%s works" % path.split("?")[0].split("/")[-1], code, 200)
    # export returns CSV, not JSON: tested on its own
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            B + "/api/export?member=default", headers={"Origin": B}), timeout=10)
        body = r.read().decode("utf-8-sig")
        head = body.split("\n")[0]
        check_true("CSV export works", r.status == 200 and head.startswith("Date"), head[:30])
        check_true("CSV export has pounds and kilograms", "Weight lb" in head and "Weight kg" in head, head[:40])
    except Exception as e:
        check_true("CSV export works", False, str(e)[:40])

    # A food in My Foods needs no key. Wait for the starter list (written just after the server starts)
    for _ in range(20):
        code, d = req("/api/food_lookup", {"action": "list"})
        if code == 200 and (d or {}).get("items"):
            break
        time.sleep(0.25)
    req("/api/food_lookup", {"action": "save", "name": "Jasmine rice",
                             "unit": "g", "per": 1.29})
    code, d = req("/api/kcal_foods", {"foods": [{"name": "Jasmine rice",
                                                 "weight": 150, "unit": "g"}]})
    check_true("My Foods hit needs no key", code == 200 and (d or {}).get("ok")
               and d.get("from") == "local", "cal=%s" % ((d or {}).get("foods") or [{}])[0].get("cal"))
    code, d = req("/api/kcal_foods", {"foods": [{"name": "Egg", "weight": 2, "unit": "each"}]})
    check_true("starter list works offline (2 eggs)", code == 200 and (d or {}).get("from") == "local"
               and ((d or {}).get("foods") or [{}])[0].get("cal") == 144,
               "cal=%s" % ((d or {}).get("foods") or [{}])[0].get("cal"))
    code, d = req("/api/kcal_foods", {"foods": [{"name": "Some dish never seen",
                                                 "weight": 100, "unit": "g"}]})
    check_true("not in My Foods and no key: explains why",
               code == 200 and ((d or {}).get("err") or (d or {}).get("note")),
               str((d or {}).get("err") or (d or {}).get("note"))[:28])

    # Hand-entered foods in piece units (slice, scoop, serving) must be found again offline.
    # The calculator's "Save to My foods" sends the unit as picked; it has to come back under the same unit.
    for name, unit, per, amt in [("Sourdough toast", "slice", 110, 2), ("Pea protein", "scoop", 100, 1),
                                 ("Chili", "serving", 300, 1)]:
        req("/api/food_lookup", {"action": "save", "name": name, "unit": unit, "per": per})
        code, d = req("/api/food_lookup", {"name": name, "unit": unit, "amount": amt})
        check_true("hand-saved \"%s\" (%s) found offline" % (name, unit),
                   code == 200 and (d or {}).get("ok") and d.get("from") == "local" and d.get("cal") == per * amt,
                   str((d or {}).get("err") or (d or {}).get("cal"))[:40])


def setup_server():
    tmp = tempfile.mkdtemp(prefix="bodycraft_degrade_")
    copy_app(tmp)
    proc = subprocess.Popen([sys.executable, SERVER], cwd=tmp,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    flag = os.path.join(tmp, ".server_running.json")
    for _ in range(40):
        time.sleep(0.3)
        if os.path.exists(flag):
            try:
                with open(flag, encoding="utf-8") as f:
                    return proc, json.load(f)["port"], tmp
            except ValueError:
                continue          # caught it half-written; read again
    proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError("server didn't start")


def main():
    m, d = load_module()
    try:
        test_guardrails(m)
        test_bmr(m)
        test_lookup_match(m)
        test_coach_context(m)
        test_llm_garbage(m)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    proc, port, tmp = setup_server()
    try:
        test_no_key(port)
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("  Degrade and guards: %d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  Failed:")
        for f in FAIL:
            print("    -", f)
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
