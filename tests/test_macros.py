"""Macros and catching up on past weights -- two things that came out of a hands-on trial.

  - Besides calories, many people watch whether they get enough protein (afraid of losing muscle and
    ending up soft rather than lean), and look at carbs and fat too
    -> every food carries protein/carbs/fat; My Foods stores them per unit; all four logging paths carry them;
       three bars under the calorie tracker, targets from the budget and body weight; the
       "Protein at every meal" habit can tick itself
  - Pasted a "weigh-ins since Aug 25" table into the coach and only today's entry was filled in
    -> the logging model returns one entry per date, the confirm panel shows one row per date, all saved at once

Usage:
    python3 test_macros.py          run everything
    python3 test_macros.py api      only the parts that don't need a browser (a few seconds)
    python3 test_macros.py page     only the browser parts
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = "bodycraft_server.py"
PAGE = "bodycraft.html"
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".macros_tmp")
PASS, FAIL = [], []

# A weigh-in log as someone would paste it from their notes app (pounds, US dates)
TABLE = """My weigh-ins since Aug 25
Aug 25 (starting point) 176.4 lb
Aug 27 174.2 lb
Early Sep 170.0-171.3 lb
Sep 23 167.1 lb
Sep 28 166.2 lb
Oct 9 163.7 lb
Oct 11 163.9 lb
Oct 12 163.3 lb"""
# The same thing copied out of a chat app: a Markdown table with pipes and bold, units only in the header
TABLE_MD = """My weigh-ins since Aug 25:

| Date | Weight (lb) |
| --- | --- |
| **Aug 25 (start)** | 176.4 |
| **Aug 27** | 174.2 |
| **Early Sep** | 170.0 - 171.3 |
| **Sep 23** | 167.1 |
| **Sep 28** | 166.2 |
| **Oct 9** | 163.7 |
| **Oct 11** | 163.9 |
| **Oct 12** | 163.3 |"""

# A 1x1 PNG
PNG_HEX = ("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cfc0f01f"
           "0005000209bb4e590000000049454e44ae426082")


def check(name, got, want, note=""):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    line = "  %-40s %s" % (name, "PASS" if ok else "FAIL")
    if not ok:
        line += "   expected %r, got %r" % (want, got)
        if note:
            line += "   " + str(note)[:300]
    print(line)
    return ok


def check_true(name, cond, note=""):
    return check(name, bool(cond), True, note)


# ---------- fake model ----------
FAKE = {"log": [], "replies": []}


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        FAKE["log"].append(body.get("messages") or [])
        reply = FAKE["replies"].pop(0) if FAKE["replies"] else "Okay."
        # A reply can be a dict: simulates a reasoning model that writes into reasoning_content and leaves content empty
        msg = dict(reply) if isinstance(reply, dict) else {"content": reply}
        finish = msg.pop("finish_reason", "stop")
        FAKE["last_max_tokens"] = body.get("max_tokens") or body.get("max_completion_tokens")
        FAKE.setdefault("models", []).append(body.get("model"))
        out = json.dumps({"choices": [{"message": msg, "finish_reason": finish}]},
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


def fresh_server(fake_port=None, old_lookup=False):
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
    if old_lookup:
        # An existing user: My Foods was seeded by the version before macros existed
        with open(os.path.join(TMP, "food_lookup.json"), "w", encoding="utf-8") as f:
            json.dump({"Egg|each": {"per": 72, "name": "Egg", "unit": "each", "note": "", "seed": True},
                       "Turkey breast deli|slice": {"per": 30, "name": "Turkey breast deli", "unit": "slice",
                                                    "note": "", "seed": True},
                       "Grandma's biscuit|each": {"per": 180, "name": "Grandma's biscuit", "unit": "each", "note": ""}},
                      f, ensure_ascii=False)
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
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=60)
    raw = r.read().decode("utf-8-sig")
    try:
        return json.loads(raw)
    except Exception:
        return raw


def load_module():
    spec = importlib.util.spec_from_file_location("svc", os.path.join(BASE, SERVER))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def lb_to_kg(lb):
    return round(lb / 2.20462, 2)


def table_weights(today_s):
    """What a well-behaved logging model returns for TABLE: one entry per date, in kg
    (early Sep -> the 3rd, the range -> its midpoint, marked approximate)."""
    return [{"date": "2026-08-25", "kg": lb_to_kg(176.4)}, {"date": "2026-08-27", "kg": lb_to_kg(174.2)},
            {"date": "2026-09-03", "kg": lb_to_kg(170.65), "approx": True}, {"date": "2026-09-23", "kg": lb_to_kg(167.1)},
            {"date": "2026-09-28", "kg": lb_to_kg(166.2)}, {"date": "2026-10-09", "kg": lb_to_kg(163.7)},
            {"date": "2026-10-11", "kg": lb_to_kg(163.9)}, {"date": today_s, "kg": lb_to_kg(163.3)}]


# ======================================================================
def part_api():
    m = load_module()
    today = datetime.date.today()
    today_s = today.isoformat()

    print("\n[Macros: from the model, from My Foods]")
    check("p/c/f count only when all three are there", m.macro_fields({"p": 6.3, "c": 0.4, "f": 5}), {"p": 6.3, "c": 0.4, "f": 5.0})
    check("one missing, none counted", m.macro_fields({"p": 6.3, "c": 0.4}), {})
    check("spelled-out field names work", m.macro_fields({"protein": "12", "carbs": 30, "fat": 8}), {"p": 12.0, "c": 30.0, "f": 8.0})
    check("a negative value voids the set", m.macro_fields({"p": 9999, "c": -3, "f": 1}), {})
    check("absurd values are capped at 500", m.macro_fields({"p": 9999, "c": 3, "f": 1}), {"p": 500.0, "c": 3.0, "f": 1.0})
    check("per-unit values are multiplied by the amount", m.scale_macros({"p": 6.3, "c": 0.4, "f": 5.0}, 2), {"p": 12.6, "c": 0.8, "f": 10.0})
    check("per-gram 0.026 isn't rounded away", m.macro_raw({"p": 0.026, "c": 0.26, "f": 0.003}), {"p": 0.026, "c": 0.26, "f": 0.003})
    check_true("every starter food has macros", all(len(x) == 7 and x[4] >= 0 and x[5] >= 0 and x[6] >= 0 for x in m.FOOD_SEED), len(m.FOOD_SEED))
    check("a meal: totals only when every food has them",
          m.meal_macros({"foods": [{"name": "Egg", "p": 12.6, "c": 0.8, "f": 10}, {"name": "Milk 2%", "p": 8, "c": 12, "f": 8}]}),
          {"p": 20.6, "c": 12.8, "f": 18.0})
    check("a meal: one food without them, no total",
          m.meal_macros({"foods": [{"name": "Egg", "p": 12.6, "c": 0.8, "f": 10}, {"name": "Takeout", "cal": 600}]}), None)
    tot, nk, na = m.day_macros({"breakfast": {"cal": 300, "foods": [{"name": "Egg", "p": 12.6, "c": 0.8, "f": 10}]},
                                "lunch": {"cal": 600, "foods": [{"name": "Takeout", "cal": 600}]},
                                "dinner": {"cal": None}})
    check("a day: 1 of 2 meals has details", (tot, nk, na), ({"p": 13, "c": 1, "f": 10}, 1, 2))
    check("protein target 1.5 g per kg (about 0.7 g per lb)", m.protein_target({}, 62), 93)
    check("a target set under Edit goals wins", m.protein_target({"protein_g": 110}, 62), 110)
    check("an absurd custom target is ignored", m.protein_target({"protein_g": 5}, 62), 93)
    check("profile cleanup: protein target 20-300", m.sanitize_profile({"protein_g": "110"}).get("protein_g"), 110)
    check("profile cleanup: blank means none", m.sanitize_profile({"protein_g": ""}).get("protein_g"), None)

    print("\n[Almost-JSON from the model is still understood]")
    keys = ["lunch", "dinner"]
    names = {"Lunch": "lunch", "Dinner": "dinner", "lunch": "lunch", "dinner": "dinner"}
    T2 = "Lunch: a 70 g bagel. Dinner was at 7pm, a small bowl of beef noodle soup"

    def run(txt, text=T2):
        d = m._extract_json(txt, want=m._DAILY_LOG_KEYS)
        return m._sanitize_daily_log(d, keys, 0, None, meal_names=names, text=text)
    c = run('{"meals": {"lunch": {"foods": [{"name": “bagel”, "weight": 70, "cal": 190,},],},}, // lunch\n /* done */}')
    check("smart quotes, trailing commas, comments all fixed", (c.get("meals") or {}).get("lunch", {}).get("cal"), 190)
    c = run("{'meals': {'lunch': {'foods': [{'name': 'bagel', 'weight': 70, 'cal': 190}]}}, 'weights': [{'date': '2026-10-01', 'kg': 75, 'approx': True}]}")
    check("single quotes and Python True fixed", ((c.get("weights") or [{}])[0].get("approx"), c.get("meals", {}).get("lunch", {}).get("cal")), (True, 190))
    c = run('{"meals":{"meal_key":{"name":"Lunch","foods":[{"name":"bagel","weight":70,"cal":190}]}}}')
    check("template placeholder copied as the key: found by name", list((c.get("meals") or {}).keys()), ["lunch"])
    c = run('{"meals":{"Lunch":{"foods":[{"name":"bagel","weight":70,"cal":190}]},"Dinner":{"at":"19:00","foods":[{"name":"Beef noodle soup","weight":300,"cal":420}]}}}')
    check("meals keyed by display name: both found", sorted((c.get("meals") or {}).keys()), ["dinner", "lunch"])
    c = run('{"meals":[{"meal":"lunch","foods":[{"name":"bagel","weight":70,"cal":190}]}]}')
    check("meals as a list also accepted", list((c.get("meals") or {}).keys()), ["lunch"])
    c = run('{"meals":{"dinner":{"foods":[{"name":"Beef noodle soup","cal":420}]}},"weight":150}')
    check("no body weight mentioned: top-level weight:150 ignored (grams of soup)", c.get("weight"), None)
    check("the prompt calls body weight body_kg", run('{"body_kg":74.05}', "Weight 74.05 kg").get("weight"), 74.05)
    check("old key weight, text mentions kg: accepted", run('{"weight":74.05}', "74.05 kg today").get("weight"), 74.05)
    check("pounds copied through unconverted are converted",
          run('{"body_kg":163.3}', "Weighed in at 163.3 lb this morning").get("weight"), lb_to_kg(163.3))
    src0 = open(os.path.join(BASE, SERVER), encoding="utf-8").read()
    tpl = src0.split("def daily_log_from_text")[1].split("def _call_llm_text")[0]
    check_true("the template has no placeholder meal key", '"meal_key"' not in tpl and '"meal key"' not in tpl
               and '{"meals":{"%s":' in tpl)

    print("\n[Claude through the OpenAI-compatible API]")
    sent = []

    class _R:
        def __init__(self, body): self.b = body
        def read(self): return self.b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return _R(json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}).encode())
    real_cfg, real_open = m.load_llm_config, m.urllib.request.urlopen
    try:
        m.urllib.request.urlopen = _fake_urlopen
        m.load_llm_config = lambda: {"base_url": "https://api.anthropic.com/v1", "api_key": "k", "model": "claude-sonnet-5-5",
                                     "vision_model": "claude-sonnet-5-5", "fast_model": ""}
        m._llm_chat([{"role": "user", "content": "hi"}], "claude-sonnet-5-5", 0.3)
        check("Claude: no temperature sent (new models only accept 1; 0.3 is a 400)", "temperature" in sent[-1], False)
        check("Claude: thinking switched off for short structured jobs", sent[-1].get("thinking"), {"type": "disabled"})
        check_true("Claude: images get a data:image prefix",
                   m._image_url("/9j/abc").startswith("data:image/jpeg;base64,") and m._image_url("iVBORabc").startswith("data:image/png;base64,"))
        check("Claude: blank logging model means Haiku", m.fast_model_of(m.load_llm_config()), "claude-haiku-5-5")
        m.load_llm_config = lambda: {"base_url": "https://api.openai.com/v1", "api_key": "k", "model": "gpt-5.1"}
        m._llm_chat([{"role": "user", "content": "hi"}], "gpt-5.1", 0.3)
        check("OpenAI: no Claude-only thinking parameter", "thinking" in sent[-1], False)
        check("OpenAI: max_tokens always sent explicitly", sent[-1].get("max_tokens"), 4096)
        check("OpenAI: blank logging model means gpt-5-nano", m.fast_model_of(m.load_llm_config()), "gpt-5-nano")
        check("a data URL from the page is passed through unchanged",
              m._image_url("data:image/png;base64,iVBORabc"), "data:image/png;base64,iVBORabc")
    finally:
        m.load_llm_config, m.urllib.request.urlopen = real_cfg, real_open
    check_true("the settings panel lists Claude", "id:'claude'" in open(os.path.join(BASE, PAGE), encoding="utf-8").read())

    print("\n[Catching up on weights: the whole table is understood]")
    check("a weigh-in table is a 'record', not chat", m.record_kind(TABLE), "record")
    check("a Markdown table (pipes, bold, unit only in the header) too", m.record_kind(TABLE_MD), "record")
    check("just 'Weight 163.3' is still a record", m.record_kind("Weight 163.3"), "record")
    raw = {"weights": [
        {"date": "2026-08-25", "kg": 80.0}, {"date": "2026-08-27", "kg": 79.0},
        {"date": "2026-09-03", "kg": 77.4, "approx": True}, {"date": "2026-09-23", "kg": 75.8},
        {"date": "2026-09-28", "kg": 75.4}, {"date": "2026-10-09", "kg": 74.25},
        {"date": "2026-10-11", "kg": 74.35}, {"date": today_s, "kg": 74.05},
        {"date": "2099-01-01", "kg": 70}, {"date": "2026-09-23", "kg": 75.8},
        {"date": "2026-07-01", "kg": 176.4}, {"date": "2026-06-01", "kg": 12}, {"date": "not a date", "kg": 70}]}
    got = m._sanitize_daily_log(raw, ["lunch"], text=TABLE)
    ws = got.get("weights") or []
    check("all rows kept; future and duplicate dates dropped", len(ws), 9)
    check("sorted by date", [w["date"] for w in ws][:3], ["2026-07-01", "2026-08-25", "2026-08-27"])
    check("two decimals kept", [w["kg"] for w in ws if w["date"] == "2026-10-09"], [74.25])
    check("'early Sep' marked as approximate", [w.get("approx") for w in ws if w["date"] == "2026-09-03"], [True])
    check("176.4 copied from the pounds table is converted to kg", [w["kg"] for w in ws if w["date"] == "2026-07-01"], [lb_to_kg(176.4)])
    check("12 kg is implausible and dropped", [w for w in ws if w["date"] == "2026-06-01"], [])
    check("today's row also becomes today's weight", got.get("weight"), 74.05)
    got2 = m._sanitize_daily_log({"weights": [{"date": "2026-08-25", "kg": 80}]}, ["lunch"])
    check("only past dates: no fake 'today' weight", got2.get("weight"), None)
    src = open(os.path.join(BASE, SERVER), encoding="utf-8").read().replace('\\"', '"')
    check_true("logging prompt teaches 'early Sep -> 3rd, mid -> 15th, end of -> 28th'",
               '"early September" -> the 3rd' in src and '"mid" -> the 15th' in src and '"end of" -> the 28th' in src
               and '"weights"' in src)

    print("\n[API: My Foods, records, export and coach all carry macros]")
    fake = start_fake()
    proc, port = fresh_server(fake.server_address[1], old_lookup=True)
    try:
        lst = api(port, "/api/food_lookup", {"action": "list"})["items"]
        egg = [x for x in lst if x["name"] == "Egg"][0]
        check("an existing user's starter egg gets its protein filled in", egg.get("p"), 6.3)
        mine = [x for x in lst if x["name"] == "Grandma's biscuit"][0]
        check("the user's own entries aren't touched", mine.get("p"), None)
        r = api(port, "/api/kcal_foods", {"foods": [{"name": "Egg", "weight": 2, "unit": "each", "unit_auto": True}]})
        check("two eggs: protein 12.6 straight from My Foods", (r["foods"][0].get("p"), r["foods"][0].get("from")), (12.6, "local"))
        api(port, "/api/food_lookup", {"action": "save", "name": "Egg", "unit": "each", "per": 75})
        egg2 = [x for x in api(port, "/api/food_lookup", {"action": "list"})["items"] if x["name"] == "Egg"][0]
        check("editing calories in My Foods keeps the macros", (egg2["per"], egg2.get("p")), (75, 6.3))
        api(port, "/api/food_lookup", {"action": "save", "name": "Turkey breast deli", "unit": "slice", "per": 32})
        items = api(port, "/api/food_lookup", {"action": "list"})["items"]
        tk = [x for x in items if x["name"] == "Turkey breast deli"]
        check("editing a 'slice' food updates that entry, unit intact",
              [(x["key"], x["unit"], x["per"], x.get("p")) for x in tk],
              [("Turkey breast deli|slice", "slice", 32, 5.0)])

        api(port, "/api/content", {"profile": {"setup_done": True, "start_weight": 62, "sex": "female",
                                               "age": 37, "height_cm": 162, "start_date": today_s}})
        d = api(port, "/api/data")
        d["members"][0].update({"name": "Sam", "start_weight": 62, "targets": [62, 56], "actual": {today_s: 62}})
        api(port, "/api/data", d)
        api(port, "/api/photo_meta", {"date": today_s, "meal": "breakfast", "member": "default", "at": "07:30",
                                      "foods": [{"name": "Egg", "weight": 2, "unit": "each", "cal": 144, "p": 12.6, "c": 0.8, "f": 10},
                                                {"name": "Milk 2%", "weight": 250, "unit": "ml", "cal": 125, "p": 8, "c": 12.5, "f": 8}],
                                      "cal": 269, "items": "Egg, Milk 2%"})
        api(port, "/api/photo_meta", {"date": today_s, "meal": "lunch", "member": "default", "at": "12:10",
                                      "foods": [{"name": "Takeout", "cal": 650}], "cal": 650, "items": "Takeout"})
        ph = api(port, "/api/photos?date=%s&member=default" % today_s)
        check("the record keeps each food's macros", ph["breakfast"]["foods"][0].get("p"), 12.6)
        check("the calories-only meal has no macros, none made up", ph["lunch"]["foods"][0].get("p"), None)
        # another member's day, so the totals above stay as they are
        api(port, "/api/photo_meta", {"date": today_s, "meal": "breakfast", "member": "m1", "at": "08:00",
                                      "foods": [{"name": "Whole wheat bread", "weight": 2, "unit": "slice", "cal": 160, "p": 8, "c": 28, "f": 2.2},
                                                {"name": "Whey protein", "weight": 1, "unit": "scoop", "cal": 120, "p": 24, "c": 3, "f": 1.5}],
                                      "cal": 280, "items": "Whole wheat bread, Whey protein"})
        ph = api(port, "/api/photos?date=%s&member=m1" % today_s)
        check("units longer than four letters survive (slice, scoop)",
              [f.get("unit") for f in ph["breakfast"]["foods"]], ["slice", "scoop"])

        FAKE["log"].clear(); FAKE["replies"][:] = ["Got it."]
        api(port, "/api/coach", {"persona": "warm", "events": [], "history": [{"role": "user", "content": "Did I get enough protein today?"}],
                                 "context": {"member": "default", "macro_targets": {"p": 93, "c": 150, "f": 40, "budget": 1500},
                                             "meals": {"breakfast": {"name": "Breakfast", "cal": 269, "source": "recorded", "items": "Egg, Milk 2%", "at": "07:30", "p": 20.6, "c": 13.3, "f": 18},
                                                       "lunch": {"name": "Lunch", "cal": 650, "source": "recorded", "items": "Takeout", "at": "12:10"}}}})
        sp = FAKE["log"][-1][0]["content"] if FAKE["log"] else ""
        up = "\n".join(x.get("content", "") for x in FAKE["log"][-1]) if FAKE["log"] else ""
        k = "Protein in meals logged with foods"
        check_true("the coach knows: 21 g protein in detailed meals, target about 93 g",
                   "Protein in meals logged with foods: 21 g; today's target about 93 g." in up,
                   up[up.find(k) - 60: up.find(k) + 100] if k in up else up[-300:])
        k = "protein 21 g"
        check_true("today's macros are in the coach's profile of the user too (remembered after reload)",
                   "protein 21 g (target about 93 g" in sp and "1 more meal(s) logged without foods aren't counted" in sp,
                   sp[sp.find(k) - 60: sp.find(k) + 160] if k in sp else sp[-300:])

        csv_txt = api(port, "/api/export?member=default")
        check_true("export has protein, carbs and fat columns", "Protein g" in csv_txt and "Carbs g" in csv_txt and "Fat g" in csv_txt)
        FAKE["replies"][:] = ["## How these days went\nGood."]
        api(port, "/api/review", {"start": today_s, "end": today_s, "member": "default"})
        rp = FAKE["log"][-1][-1]["content"] if FAKE["log"] else ""
        k = "protein 21 g"
        check_true("the review prompt has protein (1/2 meals logged with foods)",
                   "protein 21 g" in rp and "(1/2 meals logged with foods)" in rp,
                   rp[rp.find(k) - 20: rp.find(k) + 80] if k in rp else rp[-200:])

        print("\n[API: one long message, several things at once, still sorted out]")
        LONG = ("Lunch: a 70 g bagel, 18 g of American cheese, a large raw tomato, 90 g of grilled chicken breast, "
                "a 400 ml coffee with 7 g of soluble fiber, and 110 g of plain nonfat kefir. "
                "Dinner around 7pm, a small bowl of beef noodle soup")
        ANSWER = json.dumps({"meals": {
            "lunch": {"foods": [{"name": "Bagel", "weight": 70, "cal": 190, "p": 7.0, "c": 37.0, "f": 1.2},
                                {"name": "American cheese", "weight": 18, "cal": 60, "p": 3.5, "c": 1.0, "f": 5.0},
                                {"name": "Tomato", "weight": 180, "cal": 32, "p": 1.6, "c": 7.0, "f": 0.4},
                                {"name": "Grilled chicken breast", "weight": 90, "cal": 149, "p": 28.0, "c": 0.0, "f": 3.2},
                                {"name": "Coffee with fiber", "weight": 400, "cal": 20, "p": 0.0, "c": 7.0, "f": 0.0},
                                {"name": "Nonfat kefir", "weight": 110, "cal": 40, "p": 4.0, "c": 5.0, "f": 0.0}]},
            "dinner": {"at": "19:00", "foods": [{"name": "Beef noodle soup", "weight": 300, "cal": 420, "p": 18.0, "c": 60.0, "f": 10.0}]}}})
        MEALS = [{"key": "lunch", "name": "Lunch"}, {"key": "dinner", "name": "Dinner"}]
        # (1) the model restates the template before answering
        FAKE["log"].clear()
        FAKE["replies"][:] = ['Following the template: {"meals":{"meal_key":{"at":"12:00","foods":[]}},"body_kg":75.6}\n\nAnswer:\n```json\n' + ANSWER + '\n```']
        r = api(port, "/api/daily_log", {"text": LONG, "meals": MEALS})
        check("template restated first: the later answer is used", r.get("ok"), True, r.get("err"))
        check("six foods at lunch and one at dinner",
              (len(r["log"]["meals"]["lunch"]["foods"]), len(r["log"]["meals"]["dinner"]["foods"])), (6, 1))
        check("dinner at 7 comes back", r["log"]["meals"]["dinner"].get("at"), "19:00")
        check("every food has macros", r["log"]["meals"]["lunch"]["foods"][3].get("p"), 28.0)
        check("the template's body_kg isn't taken as a weight", r["log"].get("weight"), None)
        check("done in one call, no retry", len(FAKE["log"]), 1)
        # (2) a reasoning model: empty content, JSON in the reasoning
        FAKE["log"].clear()
        FAKE["replies"][:] = [{"content": "", "reasoning_content": "The user described lunch and dinner... final output: " + ANSWER}]
        r = api(port, "/api/daily_log", {"text": LONG, "meals": MEALS})
        check("empty content, JSON in reasoning: still found", r.get("ok"), True, r.get("err"))
        # (3) no JSON twice: say it's a model format problem, not that the user was unclear
        FAKE["log"].clear()
        FAKE["replies"][:] = ["Sure, let me sort out today's log for you.", "Sorry, let me try that again."]
        r = api(port, "/api/daily_log", {"text": LONG, "meals": MEALS})
        check("not JSON: asks once more", len(FAKE["log"]), 2)
        check_true("error says it's the model's format and the replies are in .llm_debug.txt",
                   "JSON" in (r.get("err") or "") and ".llm_debug.txt" in (r.get("err") or ""), r.get("err"))
        check_true("doesn't claim there was nothing to log", "didn't find any" not in (r.get("err") or ""), r.get("err"))
        check_true("the raw replies were written to disk", os.path.exists(os.path.join(TMP, ".llm_debug.txt")))
        # (5) seen on a real machine: a reasoning model spends the whole budget thinking, the answer is cut off (finish_reason=length)
        FAKE["log"].clear()
        FAKE["replies"][:] = [{"content": "", "reasoning_content": "Let me parse the user's description...\n" * 50 + '{"meals":{"lunch":{"day":0,"foods":[{"name":"Bagel","weight":70,"', "finish_reason": "length"},
                              {"content": "", "reasoning_content": "Let me analyze...\n" * 50, "finish_reason": "length"}]
        r = api(port, "/api/daily_log", {"text": LONG, "meals": MEALS})
        check_true("out of tokens: error says reasoning used it up and names a non-reasoning model",
                   "reasoning" in (r.get("err") or "") and "gpt-5-nano" in (r.get("err") or ""), r.get("err"))
        check_true("the second try gets a bigger budget (20,000+)", (FAKE.get("last_max_tokens") or 0) >= 20000, FAKE.get("last_max_tokens"))
        # (6) two models, two jobs: chat on the main model, logging on the fast one
        api(port, "/api/apikey", {"api_key": "sk-test", "base_url": "http://127.0.0.1:%d" % fake.server_address[1],
                                  "model": "gpt-5.1", "fast_model": "gpt-5-nano"})
        FAKE["models"] = []; FAKE["replies"][:] = [ANSWER]
        api(port, "/api/daily_log", {"text": LONG, "meals": MEALS})
        FAKE["replies"][:] = ["Okay."]
        api(port, "/api/coach", {"persona": "warm", "events": [], "context": {"member": "default"},
                                 "history": [{"role": "user", "content": "How's today going?"}]})
        check("logging uses the fast model, the coach the main one", FAKE["models"], ["gpt-5-nano", "gpt-5.1"])
        cfg = api(port, "/api/apikey")
        check("settings show the logging model", (cfg.get("fast_model"), cfg.get("fast_model_effective")), ("gpt-5-nano", "gpt-5-nano"))
        api(port, "/api/apikey", {"api_key": "sk-test", "base_url": "https://api.openai.com/v1", "model": "gpt-5.1"})
        check("OpenAI with no logging model set: gpt-5-nano", api(port, "/api/apikey").get("fast_model_effective"), "gpt-5-nano")
        api(port, "/api/apikey", {"api_key": "sk-test", "base_url": "https://api.anthropic.com/v1", "model": "claude-sonnet-5-5"})
        check("Claude with no logging model set: Haiku", api(port, "/api/apikey").get("fast_model_effective"), "claude-haiku-5-5")
        api(port, "/api/apikey", {"api_key": "sk-test", "base_url": "http://127.0.0.1:%d" % fake.server_address[1], "model": "fake"})
        # (4) the model returns an empty object
        FAKE["replies"][:] = ["{}"]
        r = api(port, "/api/daily_log", {"text": "Nice weather today", "meals": MEALS})
        check_true("empty object: says nothing came out of it", "Nothing came out of that" in (r.get("err") or ""), r.get("err"))

        print("\n[API: sending a picture to the coach]")
        import base64 as _b64
        png = _b64.b64encode(bytes.fromhex(PNG_HEX)).decode()
        FAKE["log"].clear()
        FAKE["replies"][:] = ['{"kind":"food","meal":"lunch","foods":[{"name":"Chicken breast","weight":120,"cal":198,"p":37.0,"c":0.0,"f":4.3},{"name":"White rice","weight":150,"cal":174,"p":3.9,"c":39.0,"f":0.5}],"note":""}']
        r = api(port, "/api/coach_image", {"data": png, "ext": "png", "text": "", "meal_names": {"lunch": "Lunch", "dinner": "Dinner"}})
        check("food photo: kind food, guessed lunch, two foods", (r.get("kind"), r.get("meal"), len(r.get("foods") or [])), ("food", "lunch", 2))
        check("with macros", r["foods"][0].get("p"), 37.0)
        check_true("the image went to the vision model (image_url in the message)",
                   any(isinstance(x.get("content"), list) for x in FAKE["log"][-1]) if FAKE["log"] else False)
        n_before = len(FAKE["log"])
        r2 = api(port, "/api/photo", {"date": today_s, "meal": "lunch", "member": "default", "data": png, "ext": "png", "foods": r["foods"]})
        check("photo saved to lunch, food list returned as is", (r2.get("ok"), len(r2.get("foods") or []), r2.get("total")), (True, 2, 372))
        check("with a food list, the vision model isn't asked again", len(FAKE["log"]), n_before)
        FAKE["replies"][:] = ['{"kind":"text","meal":"","foods":[],"text":"Aug 25 176.4 lb\\nAug 27 174.2 lb","note":""}']
        r = api(port, "/api/coach_image", {"data": png, "ext": "png", "text": "Log these for me"})
        check("text screenshot: kind text, text copied out", (r.get("kind"), "174.2" in (r.get("text") or "")), ("text", True))
        FAKE["replies"][:] = ['{"kind":"other","note":"A landscape photo"}']
        r = api(port, "/api/coach_image", {"data": png, "ext": "png"})
        check("neither: kind other", r.get("kind"), "other")
        r = api(port, "/api/coach_image", {"data": "not-base64!", "ext": "png"})
        check("broken image: an error, no crash", r.get("ok"), False)

        print("\n[API: a pasted weigh-in table goes to the logging model]")
        FAKE["log"].clear()
        FAKE["replies"][:] = [json.dumps({"weights": table_weights(today_s), "body_kg": lb_to_kg(163.3)})]
        r = api(port, "/api/daily_log", {"text": TABLE, "meals": [{"key": "lunch", "name": "Lunch"}]})
        check("logged", r.get("ok"), True, r.get("err"))
        check("all eight days come back", len((r.get("log") or {}).get("weights") or []), 8)
        pr = FAKE["log"][-1][-1]["content"] if FAKE["log"] else ""
        check_true("the prompt has the weights format and the date rules", '"weights"' in pr and '"early September"' in pr)
        check_true("the prompt says 'It is now', so the year can be inferred", "It is now " + today_s in pr)
        check_true("the prompt says pounds must be converted to kg", "180 lb" in pr and "81.6" in pr)
    finally:
        stop(proc)
        fake.shutdown()


# ======================================================================
def part_page():
    from playwright.sync_api import sync_playwright
    print("\n[Page: log two eggs, the macros fill themselves in]")
    fake = start_fake()
    proc, port = fresh_server(fake.server_address[1])
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(locale="en-US")
            pg = ctx.new_page()
            pg.clock.install(time=datetime.datetime.combine(datetime.date.today(), datetime.time(13, 0)))
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto("http://127.0.0.1:%d/" % port)
            pg.wait_for_timeout(2600)
            pg.fill("#s0Name", "Sam"); pg.select_option("#s0Sex", "female"); pg.fill("#s0Age", "37")
            pg.fill("#s0Ft", "5"); pg.fill("#s0In", "4")
            pg.fill("#s0Start", "136.7"); pg.fill("#s0Target", "123.5"); pg.fill("#s0Weeks", "16")
            pg.click("#s0Ok"); pg.wait_for_timeout(2600)
            t = pg.evaluate("macroTargets()")
            check("protein target: 136.7 lb (62 kg) x 1.5 g/kg = 93 g", t and t["p"], 93)
            check_true("fat about a quarter of the budget, carbs get the rest",
                       t and abs(t["f"] * 9 - t["budget"] * 0.27) < 10 and t["c"] * 4 + t["p"] * 4 + t["f"] * 9 <= t["budget"] + 4, t)
            mb = pg.locator("#macroBar").inner_text()
            check_true("no meals yet: the macro bar shows targets only, no zero bars",
                       "about 93" in mb and pg.locator("#macroBar .macro-row").count() == 0, mb[:160])

            # Load My Foods first (macros are filled from it when the name field loses focus)
            pg.evaluate("loadLookupTable()"); pg.wait_for_timeout(800)
            pg.click('#photoSlots [data-act="manual"][data-meal="breakfast"]'); pg.wait_for_timeout(400)
            pg.fill("#foodList .f-name", "Egg")
            pg.locator("#foodList .f-name").first.blur(); pg.wait_for_timeout(300)
            check("after the name, the unit switches to 'each'", pg.locator("#foodList .f-unit").first.input_value(), "each")
            pg.locator("#foodList input.f-w").first.fill("2"); pg.wait_for_timeout(200)
            check("two eggs: protein fills in as 12.6", pg.locator("#foodList .f-p").first.input_value(), "12.6")
            check("fat fills in as 10", pg.locator("#foodList .f-f").first.input_value(), "10")
            pg.locator("#foodList input.f-num").nth(1).fill("144")
            pg.click("#addFoodBtn"); pg.wait_for_timeout(200)
            pg.locator("#foodList .f-name").nth(1).fill("Greek yogurt"); pg.locator("#foodList .f-name").nth(1).blur(); pg.wait_for_timeout(200)
            pg.locator("#foodList input.f-w").nth(1).fill("150"); pg.wait_for_timeout(200)
            check("150 g Greek yogurt: protein 15", pg.locator("#foodList .f-p").nth(1).input_value(), "15")
            pg.locator("#foodList .food-row").nth(1).locator("input.f-num").nth(1).fill("89")
            tot = pg.locator("#modalTotal").inner_text()
            check_true("the panel total shows protein 27.6", "protein 27.6g" in tot, tot)
            pg.fill("#mealAtInput", "07:30"); pg.click("#modalOk"); pg.wait_for_timeout(1500)
            bar = pg.locator("#macroBar").inner_text()
            check_true("under the calorie tracker: protein 28 / 93 g", "28 / 93 g" in bar, bar[:200])
            segs = pg.locator("#calSegs").inner_text()
            check_true("the meal row shows this meal's protein", "28g protein" in segs, segs[:160])
            check_true("says how much is left", "to go" in bar and "65" in bar, bar[:300])
            cl = pg.locator("#checklist").inner_text()
            check_true("only one meal logged: 'Protein at every meal' not ticked yet", "meals with protein" not in cl, cl[:200])

            # Lunch with calories only: no macros made up
            pg.click('#photoSlots [data-act="manual"][data-meal="lunch"]'); pg.wait_for_timeout(400)
            pg.fill("#foodList .f-name", "Takeout"); pg.locator("#foodList input.f-num").nth(1).fill("650")
            pg.click("#modalOk"); pg.wait_for_timeout(1500)
            bar = pg.locator("#macroBar").inner_text()
            check_true("a meal without details: says only detailed ones are counted",
                       "You logged 2 meals today and 1 have food details" in bar, bar[:300])
            # Dinner, 150 g chicken breast: a second meal with protein -> the habit ticks itself
            pg.click('#photoSlots [data-act="manual"][data-meal="dinner"]'); pg.wait_for_timeout(400)
            pg.fill("#foodList .f-name", "Chicken breast"); pg.locator("#foodList .f-name").first.blur(); pg.wait_for_timeout(200)
            pg.locator("#foodList input.f-w").first.fill("150"); pg.wait_for_timeout(200)
            check("150 g chicken breast: protein 46.5", pg.locator("#foodList .f-p").first.input_value(), "46.5")
            pg.locator("#foodList input.f-num").nth(1).fill("248")
            pg.click("#modalOk"); pg.wait_for_timeout(1500)
            cl = pg.locator("#checklist").inner_text()
            check_true("both main meals have protein: 'Protein at every meal' ticks itself", "2 meals with protein" in cl, cl[:300])
            check_true("total 74 / 93", "74 / 93 g" in pg.locator("#macroBar").inner_text(), pg.locator("#macroBar").inner_text()[:160])
            # Reopen breakfast: the macros are still there
            pg.click('#photoSlots [data-act="edit"][data-meal="breakfast"]'); pg.wait_for_timeout(400)
            check("editing brings the macros back", pg.locator("#foodList .f-p").first.input_value(), "12.6")
            pg.click("#modalCancel"); pg.wait_for_timeout(300)

            print("\n[Page: a food photo and a weigh-in screenshot sent to the coach]")
            today_s = datetime.date.today().isoformat()
            png_path = os.path.join(TMP, "picture.png")
            with open(png_path, "wb") as f:
                f.write(bytes.fromhex(PNG_HEX))
            pg.evaluate("openCoach && openCoach()"); pg.wait_for_timeout(400)
            FAKE["replies"][:] = ['{"kind":"food","meal":"","foods":[{"name":"Salmon","weight":120,"cal":250,"p":25.0,"c":0.0,"f":16.0},{"name":"Broccoli","weight":100,"cal":34,"p":2.8,"c":7.0,"f":0.4}],"note":""}']
            pg.set_input_files("#coachImgFile", png_path); pg.wait_for_timeout(500)
            check_true("picked an image: an attachment strip shows above the input", pg.locator("#coachAttach").is_visible())
            pg.click("#coachSend"); pg.wait_for_timeout(2500)
            cb = pg.locator("#coachBody").inner_text()
            check_true("photo doesn't say which meal: asks first, preselecting lunch by the clock",
                       pg.locator('#coachBody button[data-mk]').count() >= 3 and "lunch" in cb.lower(), cb[-200:])
            pg.click('#coachBody button[data-mk="lunch"]'); pg.wait_for_timeout(1500)
            h3 = pg.locator(".modal-card h3").inner_text() if pg.locator(".modal-card h3").count() else ""
            check("the same confirm form as a photo, as lunch (sent at 1 pm)", h3, "Check lunch calories")
            check("both foods are there", pg.locator("#foodList .food-row").count(), 2)
            check("macros came along", pg.locator("#foodList .f-p").first.input_value(), "25")
            pg.click("#modalOk"); pg.wait_for_timeout(1500)
            ph = pg.evaluate("fetch('/api/photos?date=' + todayKey() + '&member=' + appData.active).then(r => r.json())")
            check_true("the photo was saved to lunch", bool((ph.get("lunch") or {}).get("url")), ph.get("lunch"))
            check_true("the coach window is back", pg.evaluate("document.getElementById('coachPanel').style.display !== 'none'"))
            cb = pg.locator("#coachBody").inner_text()
            check_true("the chat says what was found and how many calories", "Salmon, Broccoli" in cb and "284 cal" in cb, cb[-200:])
            # Weigh-in screenshot
            FAKE["replies"][:] = ['{"kind":"text","meal":"","foods":[],"text":"Aug 25 176.4 lb\\nSep 23 167.1 lb\\nOct 12 163.3 lb","note":""}',
                                  json.dumps({"weights": [{"date": "2026-08-25", "kg": 80.0}, {"date": "2026-09-23", "kg": 75.8},
                                                          {"date": today_s, "kg": 74.05}], "body_kg": 74.05})]
            pg.set_input_files("#coachImgFile", png_path); pg.wait_for_timeout(400)
            pg.fill("#coachInput", "Here are the weights from my phone")
            pg.click("#coachSend"); pg.wait_for_timeout(3000)
            check("the screenshot's table becomes a catch-up list, three rows", pg.locator("#dlResult .dl-w-row").count(), 3)
            pg.click("#dlCancel"); pg.wait_for_timeout(300)
            check("no page errors", errs, [])

            print("\n[Page: set your own protein target under Edit goals]")
            pg.evaluate("openGoalsEditor()"); pg.wait_for_timeout(400)
            pg.fill("#gProt", "110"); pg.click("#goOk"); pg.wait_for_timeout(900)
            check("target is now 110", pg.evaluate("macroTargets().p"), 110)
            check_true("the bar uses 110", "/ 110 g" in pg.locator("#macroBar").inner_text())
            pg.evaluate("openGoalsEditor()"); pg.wait_for_timeout(400)
            pg.fill("#gProt", "5"); pg.click("#goOk"); pg.wait_for_timeout(300)
            check_true("5 g is refused", "between 20 and 300 g" in pg.locator("#goHint").inner_text(),
                       pg.locator("#goHint").inner_text())
            pg.fill("#gProt", ""); pg.click("#goOk"); pg.wait_for_timeout(900)
            check("blank goes back to the weight-based target", pg.evaluate("macroTargets().p"), 93)
            kc = pg.evaluate("knowCards().find(c => c.id === 'muscle').short")
            check_true("the 'keep your muscle' card shows your 93 g", "93 g" in kc, kc)

            print("\n[Page: paste a weigh-in table, eight days at once]")
            today_s = datetime.date.today().isoformat()
            FAKE["replies"][:] = [json.dumps({"weights": table_weights(today_s), "body_kg": lb_to_kg(163.3)})]
            # Opening with text sorts it right away (that's the path a table pasted into the coach takes)
            pg.evaluate("openDailyLogPanel(%s)" % json.dumps(TABLE_MD)); pg.wait_for_timeout(1800)
            n = pg.locator("#dlResult .dl-w-row").count()
            check("the confirm panel has one row per date, eight rows", n, 8)
            res = pg.locator("#dlResult").inner_text()
            check_true("the heading says 8 days", "8 days" in res, res[:160])
            check_true("the 'early Sep' row is marked approximate", "is a guess" in pg.locator("#dlResult .dl-w-row").nth(2).inner_text(),
                       pg.locator("#dlResult .dl-w-row").nth(2).inner_text())
            check("dates are editable, no future dates", pg.locator("#dlResult .dl-w-date").first.get_attribute("max"), today_s)
            pg.click("#dlGo"); pg.wait_for_timeout(1800)
            act = pg.evaluate("activeMember().actual")
            check("all eight days are saved", sorted(k for k in act if k >= "2026-08-25"),
                  ["2026-08-25", "2026-08-27", "2026-09-03", "2026-09-23", "2026-09-28", "2026-10-09", "2026-10-11", today_s])
            check("two decimals kept", act.get("2026-10-09"), 74.25)
            check("today's row is today's weight (163.3 lb)", act.get(today_s), lb_to_kg(163.3))
            pg.click('#tabs button[data-t="weight"]'); pg.wait_for_timeout(700)
            pts = pg.evaluate("document.querySelectorAll('#weightChart svg circle').length")
            check_true("the chart has these points", pts >= 8, pts)
            # The plan really started at 176.4 lb (80 kg): with that start, "lost" follows the backfilled curve
            pg.evaluate("()=>{ activeMember().start_weight = 80; profile.start_weight = 80; renderLostCard(); }")
            lost = pg.locator("#statWater").inner_text()
            check_true("lost shows 13.1 lb (176.4 -> 163.3)", lost.startswith("13.1") or lost.startswith("13.2"), lost)
            check("no page errors", errs, [])
            b.close()
    finally:
        stop(proc)
        fake.shutdown()


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg not in ("", "api", "page"):
        print("usage: python3 test_macros.py [api|page]")
        return 2
    os.makedirs(TMP, exist_ok=True)
    if arg in ("", "api"):
        part_api()
    if arg in ("", "page"):
        part_page()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 60)
    print("  Macros and catch-up: %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  x " + f)
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
