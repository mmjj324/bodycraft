# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""Erase data: clear out the entries you made while trying things out and get back to a clean start.

There are three levels, from light to heavy. The lightest one is the default; if it does the job,
don't reach for a heavier one. Before anything is deleted, a full backup is made, so you can get
it back if you change your mind.

Note: check-ins, achievements and the backup copy of your weight data are stored in your
**browser**, and this script can't reach them. After it finishes, open Bodycraft and click
"Erase all data" once as well. Only when both are cleared is everything clean. The script will
remind you again at the end.

How to run: double-click it, or in this folder run
    python3 reset_data.py      (macOS / Linux)
    py reset_data.py           (Windows)
"""
import os
import json
import shutil
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))

# What each level clears, from light to heavy. Each level includes the ones before it.
LEVEL1_RECORDS = [
    ("data.json", "weight, workouts, sleep"),
    ("photos.json", "meal calories and names"),
    ("diary.json", "daily notes"),
    ("selfies.json", "progress photo index"),
    ("attach.json", "file library index"),
]
LEVEL1_DIRS = [
    ("photos", "meal photos"),
    ("selfies", "progress photos"),
    ("files", "file library"),
]
LEVEL2_CONFIG = [
    ("content.json", "routine, meals, habits, goals"),
    ("knowledge.json", "eating rules you taught the AI"),
    ("food_lookup.json", "your food calorie list"),
    ("member_setup.json", "answers from the setup wizard"),
]
LEVEL3_SECRETS = [
    ("api_config.json", "API key and provider settings"),
    ("api_key.txt", "API key (older format)"),
    ("lan_config.json", "leftover from older versions"),
]
# Cleared at every level: these are only runtime leftovers, no point keeping them
RUNTIME = [".rollback.json", ".server_running.json"]

BACKUP_DIR = os.path.join(BASE, "backups")


def _fresh_data():
    """When data.json is reset, its start weight has to match content.json.

    A hard-coded 70 kg would disagree with the settings: if your settings say 78, the app
    would think you'd already lost 8 kg.
    """
    sw, name = 70.0, "Me"
    try:
        with open(os.path.join(BASE, "content.json"), "r", encoding="utf-8") as f:
            v = (json.load(f).get("profile") or {}).get("start_weight")
        if v and 30 <= float(v) <= 250:
            sw = float(v)
    except Exception:
        pass
    name = getattr(_fresh_data, "cached_name", None) or name
    return {"active": "default",
            "members": [{"id": "default", "name": name,
                         "start_weight": sw, "targets": [], "actual": {}}]}


# What a fresh install has. The app would create these itself, but writing them now
# avoids a blank flash the first time it opens.
FRESH = {
    "data.json": None,   # built at run time by _fresh_data()
    "photos.json": {},
    "diary.json": {},
    "selfies.json": {},
    "attach.json": {},
}


def plural(n, word, many=None):
    return "%d %s" % (n, word if n == 1 else (many or word + "s"))


def human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return "%.0f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def scan(items, dirs=()):
    """See how much is in each of these, so you know what's about to be deleted."""
    found = []
    for name, desc in items:
        p = os.path.join(BASE, name)
        if not os.path.exists(p):
            continue
        detail = ""
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if name == "data.json":
                mem = d.get("members") or []
                days = sum(len(m.get("actual") or {}) for m in mem)
                ex = sum(len(m.get("exercises") or {}) for m in mem)
                detail = "%s, %s of weight, %s of workouts" % (
                    plural(len(mem), "member"), plural(days, "day"), plural(ex, "day"))
            elif isinstance(d, dict) and d:
                inner = 0
                for v in d.values():
                    inner += len(v) if isinstance(v, (dict, list)) else 1
                detail = plural(inner, "entry", "entries")
            elif not d:
                detail = "empty"
        except Exception:
            detail = human_size(os.path.getsize(p))
        found.append((name, desc, detail))
    for name, desc in dirs:
        p = os.path.join(BASE, name)
        if os.path.isdir(p):
            n = sum(len(fs) for _, _, fs in os.walk(p))
            if n:
                found.append((name + "/", desc, "%s, %s" % (plural(n, "file"), human_size(dir_size(p)))))
    return found


def backup_all():
    """Back everything up before deleting, into backups/ with a time stamp."""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, "before-reset_" + stamp)
    os.makedirs(dest, exist_ok=True)
    everything = ([n for n, _ in LEVEL1_RECORDS] + [n for n, _ in LEVEL2_CONFIG]
                  + [n for n, _ in LEVEL3_SECRETS])
    n = 0
    for name in everything:
        p = os.path.join(BASE, name)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dest, name))
            n += 1
    for name, _ in LEVEL1_DIRS:
        p = os.path.join(BASE, name)
        if os.path.isdir(p) and any(True for _ in os.walk(p)):
            try:
                shutil.copytree(p, os.path.join(dest, name), dirs_exist_ok=True)
                n += 1
            except Exception:
                pass
    return dest, n


def wipe(items, dirs=(), rebuild=True):
    # Remember the name first: below we delete the file and then rebuild it,
    # and once it's deleted we can't read it anymore.
    _fresh_data.cached_name = None
    try:
        with open(os.path.join(BASE, "data.json"), "r", encoding="utf-8") as f:
            for m in (json.load(f).get("members") or []):
                if m.get("id") == "default" and m.get("name"):
                    _fresh_data.cached_name = m["name"]
                    break
    except Exception:
        pass
    done = []
    for name, _ in items:
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            os.remove(p)
            done.append(name)
        if rebuild and name in FRESH:
            val = _fresh_data() if name == "data.json" else FRESH[name]
            with open(p, "w", encoding="utf-8") as f:
                json.dump(val, f, ensure_ascii=False, indent=2)
    for name, _ in dirs:
        p = os.path.join(BASE, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            done.append(name + "/")
    return done


def server_running():
    p = os.path.join(BASE, ".server_running.json")
    if not os.path.exists(p):
        return False
    try:
        with open(p, "r", encoding="utf-8") as f:
            info = json.load(f)
        pid = int(info.get("pid") or 0)
        if not pid:
            return False
        if os.name == "nt":
            import subprocess
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                                 capture_output=True, timeout=6)
            return str(pid) in out.stdout.decode("latin-1", "ignore")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def pause(msg="  Press Enter to close..."):
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        print()


def main():
    print()
    print("=" * 58)
    print("  Bodycraft · Erase data")
    print("=" * 58)

    if server_running():
        print()
        print("  ! The Bodycraft server is still running. Please close the Bodycraft window")
        print("    and stop the background server first. Otherwise it may write the old data")
        print("    it still has in memory right back after we delete it.")
        print("    (uninstall.py can stop it for you; it keeps your data unless you say otherwise.)")
        print()
        input("  Once it's closed, press Enter to continue (or just close this window)...")

    lv1 = scan(LEVEL1_RECORDS, LEVEL1_DIRS)
    lv2 = scan(LEVEL2_CONFIG)
    lv3 = scan(LEVEL3_SECRETS)

    if not (lv1 or lv2 or lv3):
        print("\n  There's no data in this folder. It's already clean.\n")
        pause()
        return

    row = "    %-20s %-32s %s"
    print("\n  Here's what's in this folder:\n")
    if lv1:
        print("  [Records] things you've logged")
        for n, d, x in lv1:
            print(row % (n, d, x))
    if lv2:
        print("\n  [Settings] things you've customized")
        for n, d, x in lv2:
            print(row % (n, d, x))
    if lv3:
        print("\n  [Keys] your API key")
        for n, d, x in lv3:
            print(row % (n, d, x))

    print("""
  How much do you want to clear?

    1  Records only         Weight, meals, workouts, sleep, notes, photos.
                            Your routine, meal plan, habits and API key all stay.
                            Pick this if you've been testing and want to start logging fresh.

    2  Records + settings   Also your routine, meal plan, habits, eating rules
                            and food calorie list. Back to just-installed, but your
                            API key stays.

    3  Everything           Also your API key. Exactly like a fresh download.

    0  Cancel
""")
    choice = input("  Type a number and press Enter: ").strip()
    if choice not in ("1", "2", "3"):
        print("\n  Canceled. Nothing was changed.\n")
        pause()
        return

    label = {"1": "records", "2": "records + settings",
             "3": "everything (including your API key)"}[choice]
    print("\n  About to clear: %s" % label)
    print("  A full backup goes into the backups/ folder first, so you can get it back.")
    sure = input("\n  Are you sure? Type yes to continue, anything else to cancel: ").strip().lower()
    if sure != "yes":
        print("\n  Canceled. Nothing was changed.\n")
        pause()
        return

    dest, n = backup_all()
    print("\n  OK  Backed up %s to: %s" % (plural(n, "item"), os.path.relpath(dest, BASE)))

    removed = wipe(LEVEL1_RECORDS, LEVEL1_DIRS)
    if choice in ("2", "3"):
        removed += wipe(LEVEL2_CONFIG)
    if choice == "3":
        removed += wipe(LEVEL3_SECRETS)
    for name in RUNTIME:
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            os.remove(p)
            removed.append(name)

    print("  OK  Cleared %s" % plural(len(removed), "item"))
    print()
    print("  " + "!" * 50)
    print("  One more step: check-ins and achievements are stored in your")
    print("  browser, and this script can't delete them.")
    print()
    print("  Open Bodycraft, click \"Erase all data\" in the toolbar, and confirm.")
    print("  That clears what's saved in the browser too.")
    print("  " + "!" * 50)
    print()
    print("  The files on this computer are clean now.")
    if choice == "1":
        print("  Your routine, meal plan, habits and API key are still there.")
    elif choice == "2":
        print("  You'll go through the first-time setup wizard again. Your API key is still there.")
    else:
        print("  You'll go through the setup wizard again and need to enter your API key again.")
    print()
    pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Canceled.\n")
    except Exception as e:
        print("\n  Something went wrong: %s" % e)
        pause()
