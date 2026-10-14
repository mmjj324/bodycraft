"""Run every test -- one command, eleven layers in order.

    python3 tests/run_tests.py            everything (11 layers)
    python3 tests/run_tests.py fast       only the layers that don't need a browser (seconds)
    python3 tests/run_tests.py dataflow   only the layer(s) whose name contains this word

The point of the layers: when something fails, you know at once what kind of problem it is.
After changing code, run at least "fast"; before committing, run everything.
"""
import glob
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
APP = BASE
SERVER = "bodycraft_server.py"


def t(name, *args):
    """Command for one test script in tests/."""
    return [sys.executable, os.path.join(HERE, name)] + list(args)


SYNTAX = """
import ast, io, os, sys, glob
from html.parser import HTMLParser
bad = 0
for f in sorted(glob.glob('*.py') + glob.glob(os.path.join('tests', '*.py'))):
    try:
        ast.parse(io.open(f, encoding='utf-8').read(), filename=f)
    except Exception as e:
        print('  FAIL', f, e); bad += 1
html = ['bodycraft.html'] + [f for f in ['USER_GUIDE.html'] if os.path.exists(f)]
for f in html:
    try:
        HTMLParser().feed(io.open(f, encoding='utf-8').read())
    except Exception as e:
        print('  FAIL', f, e); bad += 1
print('  everything parses (%d files)' % (len(glob.glob('*.py')) + len(glob.glob(os.path.join('tests', '*.py'))) + len(html))
      if not bad else '  %d file(s) with problems' % bad)
sys.exit(1 if bad else 0)
"""

# (name, what it asks, command, working folder, part of "fast")
LAYERS = [
    ("integrity", "guard rails: were any functions or elements deleted by mistake",
     t("check.py"), BASE, True),
    ("unit", "pure functions: guards, parsing, merging, writing to disk",
     t("test_unit.py"), APP, True),
    ("syntax", "do the Python files and the HTML parse",
     [sys.executable, "-c", SYNTAX], APP, True),
    ("degrade", "does it work without AI, do the guards hold",
     t("test_degrade.py"), BASE, True),
    ("dataflow", "change A -- does B follow",
     t("test_dataflow.py"), BASE, False),
    ("storage", "what happens when the file and the browser disagree",
     t("test_storage.py"), BASE, False),
    ("repeat", "do the same thing twice -- is it still right",
     t("test_repeat.py"), BASE, False),
    ("eating-api", "are meal times kept, are hard moments heard, is this person in the prompt",
     t("test_eating.py", "api"), BASE, True),
    ("eating-page", "is the eating window still there after a reload, does a rough day get a form or a reply",
     t("test_eating.py", "page"), BASE, False),
    ("share-api", "pounds and kilograms, feet and inches, under-18s, the pace of big goals, DST, a member's own body",
     t("test_share.py", "api"), BASE, True),
    ("share-page", "wizard in pounds and feet, adding a member, plan end, HEIC photos, font size",
     t("test_share.py", "page"), BASE, False),
    ("macros-api", "do protein, carbs and fat follow the food, is a whole pasted weight log recognized",
     t("test_macros.py", "api"), BASE, True),
    ("macros-page", "do two logged eggs bring their macros along, do eight days of catch-up weights reach the chart",
     t("test_macros.py", "page"), BASE, False),
]


def _kill_pid(pid):
    try:
        os.kill(int(pid), signal.SIGKILL)
        return True
    except Exception:
        return False


def kill_leftover_servers():
    """Stop servers left behind by an earlier, interrupted run.

    First by the PID each copy wrote into its .server_running.json (the project folder and
    any leftover test copies inside it), then by command line. Only done here, before the first
    layer starts: no test is running yet, so nothing of ours gets hit.
    """
    flags = [os.path.join(BASE, ".server_running.json")]
    flags += glob.glob(os.path.join(BASE, ".*_tmp", ".server_running.json"))
    flags += glob.glob(os.path.join(HERE, ".*_tmp", ".server_running.json"))
    for flag in flags:
        try:
            with open(flag, encoding="utf-8") as f:
                pid = int(json.load(f).get("pid") or 0)
        except Exception:
            continue
        if pid and pid != os.getpid():
            _kill_pid(pid)
    subprocess.run(["pkill", "-9", "-f", SERVER], capture_output=True)
    time.sleep(1)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    only_fast = arg == "fast"
    if only_fast:
        picked = [x for x in LAYERS if x[4]]
    else:
        picked = [x for x in LAYERS if (not arg or arg in x[0])]
    if not picked:
        print("No layer matches %r. Choose from: fast, %s" % (arg, ", ".join(x[0] for x in LAYERS)))
        return 2

    # Clear leftovers once before starting. No test is running yet, so this can't hit one of ours.
    kill_leftover_servers()

    results = []
    t0 = time.time()
    logdir = os.path.join(HERE, ".last_run")
    os.makedirs(logdir, exist_ok=True)
    for name, desc, cmd, cwd, fast in picked:
        print("\n" + "-" * 62)
        print("  %s -- %s" % (name, desc))
        print("-" * 62)
        t1 = time.time()
        # No pkill here: `pkill -f bodycraft_server.py` also matches a test process that is
        # still running (its command line can contain the same words). The symptom is "passes
        # alone, fails in a full run", with a normal exit code and all-PASS output -- very hard
        # to track down. Each layer stops its own temporary server in a finally block; we only
        # leave a short gap between layers.
        time.sleep(1.0)
        if not os.path.exists(cmd[1]) and cmd[1] != "-c":
            out, ok = "  missing: %s" % cmd[1], False
        else:
            try:
                r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
                out, ok = (r.stdout or "") + (r.stderr or ""), r.returncode == 0
            except subprocess.TimeoutExpired as e:
                out = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) \
                    + "\n  timed out after 600 s"
                ok = False
        # Keep each layer's full output. In a full run an occasional failure only shows up as
        # "FAIL" in the summary, not which check -- with the log you can look afterwards;
        # without it, an intermittent failure means rerunning and hoping.
        with open(os.path.join(logdir, name + ".log"), "w", encoding="utf-8") as f:
            f.write(out)
        # Full output only on failure; on success, just the tail
        if ok:
            tail = [l for l in out.strip().split("\n") if l.strip()][-3:]
            print("\n".join("  " + l.strip() for l in tail))
        else:
            print(out.strip()[-3000:])
        results.append((name, ok, time.time() - t1))

    print("\n" + "=" * 62)
    print("  Test summary")
    print("=" * 62)
    for name, ok, sec in results:
        print("  %-14s %s   %.1fs" % (name, "PASS" if ok else "FAIL", sec))
    bad = [n for n, ok, _ in results if not ok]
    print("-" * 62)
    if bad:
        print("  %d layer(s) failed: %s" % (len(bad), ", ".join(bad)))
        print("  Full output is in tests/.last_run/<layer>.log")
        print("\n  When something fails, suspect the test first: three false alarms in this suite")
        print("  were mistakes in the test scripts, and nearly led to breaking working features.")
        print("  Make sure it's a product problem before you touch the code.")
    else:
        print("  All %d layers passed in %.1fs" % (len(results), time.time() - t0))
    print("=" * 62)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
