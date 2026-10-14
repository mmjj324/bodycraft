"""Repetition and order tests -- do the same thing twice, is it still right.

A whole class of bugs that slipped through were "right the first time, wrong the second":
a second workout overwrote the first, the amount box lost focus after every keystroke,
saving the same recipe twice made two entries. Doing an action once never finds these.

Three patterns:
  repeat -- the same action two or three times; assert things add up / de-duplicate as intended
  typing -- several characters in a row into one input; assert the value is complete (focus)
  order  -- delete then add, add then delete, come back after a reload

Weights are typed in pounds (the default unit) and must be stored in kilograms.

Usage: python3 tests/test_repeat.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = BASE
SERVER = "bodycraft_server.py"
LB = 2.20462
TMP = None          # temp copy of the app, set by setup_env()

# Personal data and runtime files are never copied into a test copy
PRIVATE = {".server_running.json", ".widget_running.json", ".rollback.json", ".llm_debug.txt",
           "api_config.json", "api_key.txt", "data.json", "content.json", "diary.json",
           "photos.json", "attach.json", "selfies.json", "knowledge.json", "food_lookup.json",
           "favorites.json", "member_setup.json"}

PASS, FAIL = [], []


def check(name, got, want, note=""):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    line = "  %-42s %s" % (name, "PASS" if ok else "FAIL")
    if not ok:
        line += "   expected %r, got %r" % (want, got)
    if note:
        line += "   " + note
    print(line)
    return ok


FAKE_LLM = '''
def _call_llm_text(prompt, max_tokens=4096, reasoning_ok=False, timeout=90, fast=False):
    import os as _o, json as _j
    if not _o.environ.get("FAKE"):
        return ""
    if "three ideas" in prompt:
        return _j.dumps({"ideas":[
          {"emoji":"\\U0001F372","title":"Beef and tomato stew","desc":"Simmer for forty minutes.","cal":480,
           "tags":["stew"],"items":[{"name":"Beef chuck","amount":"5 oz","cal":330}]},
          {"emoji":"\\U0001F957","title":"Chicken salad","desc":"Pan-sear, slice, toss with greens.","cal":320,
           "tags":["quick"],"items":[{"name":"Chicken breast","amount":"5 oz","cal":248}]}]})
    if "Estimate the calories for these foods" in prompt:
        return _j.dumps({"foods":[{"name":"White rice","weight":150,"cal":195}]})
    if "Estimate calories and macros for this food" in prompt:
        return _j.dumps({"per":116,"note":"cooked"})
    if "Decide which kind of message this is" in prompt:
        return _j.dumps({"intent":"chat"})
    return "{}"


def _call_llm_messages(messages):
    import os as _o
    if _o.environ.get("FAKE"):
        return "Sounds good."
    return None


def _llm_chat(messages, model, temperature=0.3, timeout=90, max_tokens=4096, reasoning_ok=False):
    # coach chats call _llm_chat directly, not the two above; catch it here too
    import os as _o
    if _o.environ.get("FAKE"):
        return "Sounds good."
    return None
'''


def setup_env():
    global TMP
    TMP = tempfile.mkdtemp(prefix="bodycraft_repeat_")
    for f in os.listdir(SRC):
        p = os.path.join(SRC, f)
        if os.path.isfile(p) and f not in PRIVATE and not f.endswith((".pyc", ".tmp")):
            shutil.copy(p, os.path.join(TMP, f))
    # Inject a fake model: this layer tests repetition and order and shouldn't depend on the network
    svc = os.path.join(TMP, SERVER)
    with open(svc, "r", encoding="utf-8") as f:
        s = f.read()
    # The injection relies on matching these three definitions exactly. If a server signature
    # changes and this file doesn't follow, the fake silently fails to go in, the tests talk to
    # the real network, and what you see is a timeout or "no answer" -- it looks like the product
    # broke when really the test stopped working. So a failed match blows up right here.
    s_box = [s]

    def swap(old, new_):
        if old not in s_box[0]:
            raise RuntimeError(
                "Fake model injection failed; this line wasn't found:\n  %s\n"
                "The server's function signature has probably changed. Update the function of the "
                "same name in FAKE_LLM in this file, together with the anchor lines below." % old)
        s_box[0] = s_box[0].replace(old, new_, 1)

    swap("def _call_llm_text(prompt, max_tokens=4096, reasoning_ok=False, timeout=90, fast=False):",
         FAKE_LLM.strip() + "\n\n\ndef _unused_call_llm_text(prompt, max_tokens=4096, reasoning_ok=False, timeout=90, fast=False):")
    swap("def _call_llm_messages(messages):", "def _unused_call_llm_messages(messages):")
    swap("def _llm_chat(messages, model, temperature=0.3, timeout=90, max_tokens=4096, reasoning_ok=False):",
         "def _unused_llm_chat(messages, model, temperature=0.3, timeout=90, max_tokens=4096, reasoning_ok=False):")
    s = s_box[0]
    with open(svc, "w", encoding="utf-8") as f:
        f.write(s)
    with open(os.path.join(TMP, "api_config.json"), "w", encoding="utf-8") as f:
        json.dump({"api_key": "k", "base_url": "https://x",
                   "model": "m", "vision_model": "v"}, f)
    env = dict(os.environ, FAKE="1")
    proc = subprocess.Popen([sys.executable, SERVER], cwd=TMP, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    flag = os.path.join(TMP, ".server_running.json")
    for _ in range(40):
        time.sleep(0.3)
        if os.path.exists(flag):
            try:
                with open(flag, encoding="utf-8") as f:
                    return proc, json.load(f)["port"]
            except ValueError:
                continue          # caught it half-written; read again
    proc.kill()
    raise RuntimeError("server didn't start")


def disk(fname):
    p = os.path.join(TMP, fname)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def run(pg, port):
    pg.goto("http://127.0.0.1:%d/" % port)
    pg.wait_for_timeout(2600)
    # The wizard in its default units: pounds, feet and inches
    pg.fill("#s0Name", "Sam")
    if pg.locator("#s0Ft").is_visible():
        pg.fill("#s0Ft", "5")
        pg.fill("#s0In", "7")
    else:
        pg.fill("#s0Height", "170")
    pg.fill("#s0Start", "172")
    pg.fill("#s0Target", "159")
    pg.fill("#s0Weeks", "16")
    pg.click("#s0Ok")
    pg.wait_for_timeout(2400)
    prof = (disk("content.json") or {}).get("profile") or {}
    print("\n[Setup - wizard in pounds and feet]")
    check("5 ft 7 in stored as 170 cm", prof.get("height_cm"), 170)
    check("172 lb start stored as kg", round(prof.get("start_weight") or 0, 1), 78.0)
    check("unit preference saved as lb", prof.get("weight_unit"), "lb")

    tab = lambda n: (pg.click("nav.tabs button[data-t='%s']" % n), pg.wait_for_timeout(420))

    print("\n[Repeat - the same action several times]")
    tab("today")
    for typ, mins in [("Walk", 40), ("Run", 25), ("Strength", 30)]:
        pg.select_option("#exType", typ)
        pg.fill("#exDur", str(mins))
        pg.click("#exSave")
        pg.wait_for_timeout(800)
        if pg.locator("#exOk").count():
            pg.click("#exOk")
            pg.wait_for_timeout(950)
    arr = list((disk("data.json")["members"][0].get("exercises") or {}).values())
    check("three workouts, three entries", len(arr[0]) if arr else 0, 3, "<- a later one used to overwrite the earlier")
    check("three durations add up", sum(x["duration"] for x in arr[0]) if arr else 0, 95)

    tab("weight")
    pg.fill("#wInput", "168.4")
    pg.click("#wSave")
    pg.wait_for_timeout(1000)
    pg.fill("#wInput", "168.0")
    pg.click("#wSave")
    pg.wait_for_timeout(1000)
    ac = disk("data.json")["members"][0]["actual"]
    check("two weigh-ins on one day keep one entry", len(ac), 1, "weight overwrites, unlike workouts")
    last = list(ac.values())[0] if ac else None
    check("same day keeps the last weigh-in", round(last * LB, 1) if last else None, 168.0,
          "stored %r kg" % last)
    check("pounds are stored as kilograms", last is not None and 75 < last < 77, True,
          "168 lb is 76.2 kg; 168 stored as-is would read as 370 lb")

    tab("today")
    pg.fill("#diaryText", "first version")
    pg.click("#diarySaveBtn")
    pg.wait_for_timeout(900)
    pg.fill("#diaryText", "second version")
    pg.click("#diarySaveBtn")
    pg.wait_for_timeout(900)
    dj = disk("diary.json") or {}
    days = dj.get("default", {})
    check("journal saved twice keeps one entry", len(days), 1)
    check("journal keeps the last save", list(days.values())[0] if days else None, "second version")

    print("\n[Typing - input focus]")
    slot = pg.locator(".photo-slot").filter(has_text="Lunch").first
    slot.locator("[data-act='manual']").click()
    pg.wait_for_timeout(700)
    w = pg.locator(".food-row .f-w").first
    w.click()
    for ch in "12345":
        pg.keyboard.type(ch)
        pg.wait_for_timeout(90)
    check("five digits typed in a row stay whole", w.input_value(), "12345", "<- focus used to drop after each key")
    for _ in range(5):
        pg.keyboard.press("Backspace")
        pg.wait_for_timeout(80)
    check("five backspaces clear it", w.input_value(), "")
    nm = pg.locator(".food-row .f-name").first
    nm.click()
    for ch in "Beef stew":
        pg.keyboard.type(ch)
        pg.wait_for_timeout(90)
    check("food name typed in a row stays whole", nm.input_value(), "Beef stew")
    pg.click("#modalCancel")
    pg.wait_for_timeout(500)

    print("\n[Repeat - saved recipes de-duplicate]")
    tab("menu")
    pg.locator(".idea-go[data-slot='lunch']").click()
    pg.wait_for_timeout(1500)
    pg.locator("#ideaLunch .fav").first.click()
    pg.wait_for_timeout(1100)
    n1 = len((disk("favorites.json") or {}).get("default", []))
    pg.locator(".idea-go[data-slot='dinner']").click()
    pg.wait_for_timeout(1500)
    pg.locator("#ideaDinner .fav").first.click()
    pg.wait_for_timeout(1100)
    favs = (disk("favorites.json") or {}).get("default", [])
    check("first save stored the recipe", n1, 1)
    check("same recipe saved twice isn't duplicated", len(favs), n1, "same title updates instead of piling up")

    print("\n[Repeat - My Foods hit]")
    tab("plate")
    # The starter list has the everyday foods, so white rice hits locally the first time.
    # Use one the starter list doesn't have to check "first time asks AI, second time is local".
    pg.fill("#lkName", "Chicken tikka masala")
    pg.select_option("#lkUnit", "g")
    pg.fill("#lkAmt", "100")
    pg.click("#lkGo")
    pg.wait_for_timeout(1500)
    first = pg.inner_text("#lkOut")
    pg.fill("#lkAmt", "200")
    pg.click("#lkGo")
    pg.wait_for_timeout(1400)
    second = pg.inner_text("#lkOut")
    check("first lookup asks AI", "saved to My foods" in first, True, first[:60].replace("\n", " "))
    check("second lookup is local", "From My foods" in second, True, "the longer you use it, the less it asks AI")
    check("local hit scales the amount", "232 cal" in second, True, "116 cal per 100 g x 200 g")

    print("\n[Order - delete then add, add then delete]")
    tab("today")
    before = pg.locator(".ex-del").count()
    pg.locator(".ex-del").first.click()
    pg.wait_for_timeout(1000)
    check("delete one, list shrinks by one", pg.locator(".ex-del").count(), before - 1)
    pg.select_option("#exType", "Swim")
    pg.fill("#exDur", "35")
    pg.click("#exSave")
    pg.wait_for_timeout(800)
    if pg.locator("#exOk").count():
        pg.click("#exOk")
        pg.wait_for_timeout(950)
    check("can still add after deleting", pg.locator(".ex-del").count(), before)
    arr2 = list((disk("data.json")["members"][0].get("exercises") or {}).values())[0]
    check("the new one is there", any(x["type"] == "Swim" for x in arr2), True)
    check("the deleted one didn't come back", sum(1 for x in arr2 if x["type"] == "Walk"), 0)

    print("\n[Order - delete everything, then start over]")
    while pg.locator(".ex-del").count():
        pg.locator(".ex-del").first.click()
        pg.wait_for_timeout(750)
    d = disk("data.json")["members"][0].get("exercises") or {}
    check("emptied day's key is removed", d, {}, "an empty list left behind breaks \"any exercise today?\"")
    pg.select_option("#exType", "Walk")
    pg.fill("#exDur", "20")
    pg.click("#exSave")
    pg.wait_for_timeout(800)
    if pg.locator("#exOk").count():
        pg.click("#exOk")
        pg.wait_for_timeout(950)
    arr3 = list((disk("data.json")["members"][0].get("exercises") or {}).values())
    check("logging works again after emptying", len(arr3[0]) if arr3 else 0, 1)

    print("\n[Order - still there after a reload]")
    pg.reload()
    pg.wait_for_timeout(2800)
    if pg.locator("#s0Skip").count():
        pg.click("#s0Skip")
        pg.wait_for_timeout(1300)
    tab("today")
    check("workout still there after reload", pg.locator(".ex-del").count(), 1)
    check("journal still there after reload", pg.input_value("#diaryText"), "second version",
          "<- it used to be read without the date and came back empty")
    tab("weight")
    check("weight unit is still pounds after reload", (pg.inner_text("#wUnitBtn") or "").strip().lower().startswith("lb"),
          True, "button reads %r" % (pg.inner_text("#wUnitBtn") or "").strip())

    print("\n[Order - opening and closing panels repeatedly]")
    tab("today")
    for _ in range(3):
        pg.click("#coachBtn")
        pg.wait_for_timeout(450)
        pg.click("#coachClose")
        pg.wait_for_timeout(350)
    pg.click("#coachBtn")
    pg.wait_for_timeout(600)
    check("coach opened and closed leaves no copies", pg.locator("#coachPanel").count(), 1)
    egs = pg.locator("#coachChips .coach-eg").count()
    check("example prompts not bound twice", egs <= 8, True, "%d present" % egs)
    # The examples stay after chatting (they used to sit in the message area and vanished with the first reply)
    pg.fill("#coachInput", "hello there")
    pg.click("#coachSend")
    pg.wait_for_timeout(1800)
    check("examples still there after chatting", pg.locator("#coachChips .coach-eg").count(), egs)
    bubbles = pg.eval_on_selector_all("#coachBody > div", "e=>e.map(x=>x.textContent.trim())")
    check("the user's message appears once", bubbles.count("hello there"), 1,
          "<- it used to go into history twice and show two bubbles | messages: %s"
          % pg.inner_text("#coachBody")[:60].replace("\n", " "))
    pg.click("#coachClose")
    pg.wait_for_timeout(400)
    for _ in range(3):
        pg.locator("#panel-today .edit-gear").first.click()
        pg.wait_for_timeout(350)
        pg.locator("body").click(position={"x": 900, "y": 150})
        pg.wait_for_timeout(300)
    check("gear menus don't stack", pg.locator(".edit-pop").count(), 0)


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
            try:
                run(pg, port)
            except Exception as e:
                check("test ran to the end", str(e).split("\n")[0][:200], "")
            errs = pg.evaluate("()=>(typeof _errLog!=='undefined')?_errLog:[]")
            check("safe() swallowed no exceptions", errs, [])
            b.close()
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    check("no JS errors throughout", js_errors, [])
    print("\n" + "=" * 58)
    print("  Repeat and order: %d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  Failed:")
        for f in FAIL:
            print("    -", f)
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
