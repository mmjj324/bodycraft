# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""Bodycraft · local data service: serves the app and reads/writes your records.
Listens on 127.0.0.1 only. Nothing is exposed to your network.
"""
import os
import re
import json
import csv
import io
import base64
import tempfile
import threading
import datetime
import urllib.parse
import urllib.request
import urllib.error
import html.parser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
# Build id. The page carries the same string; if they differ, an old server is still running
# (autostart uses pythonw with no window, so replacing files alone won't restart it).
BUILD = "1.0.0"

DATA_FILE = os.path.join(BASE, "data.json")
BACKUP_DIR = os.path.join(BASE, "backups")
PHOTO_DIR = os.path.join(BASE, "photos")
PHOTO_META_FILE = os.path.join(BASE, "photos.json")
DIARY_FILE = os.path.join(BASE, "diary.json")
SETUP_FILE = os.path.join(BASE, "member_setup.json")
CONTENT_FILE = os.path.join(BASE, "content.json")
RUN_FLAG = os.path.join(BASE, ".server_running.json")
ATTACH_DIR = os.path.join(BASE, "files")
ATTACH_META = os.path.join(BASE, "attach.json")
API_KEY_FILE = os.path.join(BASE, "api_key.txt")
API_CONFIG_FILE = os.path.join(BASE, "api_config.json")
ROLLBACK_FILE = os.path.join(BASE, ".rollback.json")
PORT = 8765

# Meals are user-editable (supper, afternoon snack, post-workout...).
# Custom meal keys live in content.json "meals"; the server allows them from there.
# Still validated: the key ends up in file names.
MEALS_DEFAULT = ["breakfast", "lunch", "dinner", "snack"]
_MEAL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
MEAL_NAMES = {"breakfast": "Breakfast", "lunch": "Lunch", "dinner": "Dinner", "snack": "Snack"}


def _now_text():
    """"Now" for the logging model, so it can tell "last night at 7" from "tonight at 7"."""
    n = datetime.datetime.now()
    return n.strftime("%Y-%m-%d %H:%M (%A)")


def clean_hm(x):
    """Normalize "12:5" / "7:30" to HH:MM; returns "" for anything that isn't a valid time.
    Meal times drive the eating-window math, so normalize once at the door.
    """
    s = str(x or "").strip().replace("\uff1a", ":").replace(".", ":")
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", s)
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return "%02d:%02d" % (h, mi)
    return ""


def meal_label(key):
    """Meal key -> display name. Prompts should say "Lunch", not "lunch_2"; custom
    meals take their name from content.json."""
    if key in MEAL_NAMES:
        return MEAL_NAMES[key]
    try:
        for m in (load_content().get("meals") or []):
            if isinstance(m, dict) and m.get("key") == key and m.get("name"):
                return str(m["name"])[:12]
    except Exception:
        pass
    return str(key)


_TOKEN_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alikes (i l 1 o 0)


def is_loopback(ip):
    return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or str(ip).startswith("127.")


def is_private(ip):
    """True for addresses that shouldn't be reachable from the public internet.
    Wider than the three textbook private ranges: also carrier-grade NAT (100.64/10)
    and the documentation ranges containers and VMs often use.
    Used when fetching a recipe URL (see check_fetch_url): a link on a page
    must not be able to make this machine poke at your router.
    """
    ip = str(ip or "")
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    if is_loopback(ip):
        return True
    parts = ip.split(".")
    if len(parts) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in parts):
        a_, b_, c_ = int(parts[0]), int(parts[1]), int(parts[2])
        if a_ == 10:
            return True
        if a_ == 192 and b_ == 168:
            return True
        if a_ == 172 and 16 <= b_ <= 31:
            return True
        if a_ == 169 and b_ == 254:
            return True
        if a_ == 100 and 64 <= b_ <= 127:
            return True
        if a_ == 192 and b_ == 0 and c_ == 2:
            return True
        if a_ == 198 and b_ in (18, 19):
            return True
        if a_ == 198 and b_ == 51 and c_ == 100:
            return True
        if a_ == 203 and b_ == 0 and c_ == 113:
            return True
        return False
    low = ip.lower()
    return low.startswith(("fe80:", "fd", "fc"))


KNOW_FILE = os.path.join(BASE, "knowledge.json")
KNOW_MAX = 6000          # max characters of user notes sent to the model


def load_knowledge():
    """Diet methods / principles the user taught the assistant.
    Stored as plain text and sent along with requests, so the advice follows
    the user's own approach instead of generic defaults.
    """
    try:
        with open(KNOW_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_knowledge(d):
    with _lock:
        write_json(KNOW_FILE, d)


def knowledge_text(member="default"):
    """One block of text for the model. Enabled entries only."""
    d = load_knowledge()
    items = [x for x in (d.get(safe_member(member)) or []) if x.get("enabled")]
    if not items:
        return ""
    out = []
    used = 0
    for it in items:
        body = str(it.get("text") or "").strip()
        if not body:
            continue
        head = "[%s]\n" % str(it.get("title") or "My method")[:40]
        room = KNOW_MAX - used
        if room <= 100:
            break
        chunk = head + body[:room - len(head)]
        out.append(chunk)
        used += len(chunk)
    if not out:
        return ""
    return ("These are the eating methods or principles the user follows. Follow them when giving advice; "
            "where generic advice conflicts, theirs wins:\n\n" + "\n\n".join(out) + "\n\n")


SELFIE_DIR = os.path.join(BASE, "selfies")
SELFIE_META = os.path.join(BASE, "selfies.json")
_SELFIE_RE = re.compile(r"^[a-z0-9_]{1,24}$")


def load_selfies():
    try:
        with open(SELFIE_META, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_selfies(d):
    with _lock:
        write_json(SELFIE_META, d)


def selfie_list(member):
    """All progress photos for a member, newest first."""
    m = safe_member(member)
    all_d = load_selfies().get(m) or {}
    out = []
    for date in sorted(all_d.keys(), reverse=True):
        for item in (all_d[date] or []):
            if isinstance(item, dict) and item.get("file"):
                out.append({"date": date, "file": item["file"],
                            "url": "/selfies/" + item["file"],
                            "note": item.get("note", ""),
                            "weight": item.get("weight")})
    return out


def allowed_meals():
    """Default meals plus any custom meals from content.json."""
    keys = list(MEALS_DEFAULT)
    try:
        for m in (load_content().get("meals") or []):
            k = (m or {}).get("key") if isinstance(m, dict) else None
            if isinstance(k, str) and _MEAL_KEY_RE.match(k) and k not in keys:
                keys.append(k)
    except Exception:
        pass
    return keys
PHOTO_EXTS = ["jpg", "jpeg", "png", "webp", "gif", "heic"]

# Default model config (OpenAI-compatible API). No provider is preselected:
# the user picks one in "AI settings". Ollama needs no key.
DEFAULT_LLM = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "vision_model": "",
    "fast_model": "",      # model for structured logging; empty = see fast_model_of()
}


def fast_model_of(cfg):
    """Which model does the structured work (parsing meals, intent, calorie lookups).
    If the user set one, use it. Otherwise pick the provider's small, fast, non-reasoning
    model: reasoning models spend thousands of tokens thinking about a bagel before writing
    any JSON and run out of room. Unknown providers fall back to the main model."""
    fm = (cfg.get("fast_model") or "").strip()
    if fm:
        return fm
    base = (cfg.get("base_url") or "")
    if "anthropic.com" in base:
        return "claude-haiku-5-5"
    if "api.openai.com" in base:
        return "gpt-5-nano"
    if "generativelanguage.googleapis.com" in base:
        return "gemini-2.5-flash-lite"
    return (cfg.get("model") or DEFAULT_LLM["model"]).strip()

_lock = threading.RLock()

# ---------- safety helpers ----------
MAX_BODY = 40 * 1024 * 1024   # request body cap 40 MB (30 MB attachment + base64 overhead)

# Allowed member ids: "default", or "m" + digits (the page generates 'm' + Date.now())
_MEMBER_RE = re.compile(r"^(default|m\d{1,20})$")


def safe_member(value):
    """Sanitize the member parameter; anything unexpected becomes "default".
    The member id goes into file names (photos, attachments), so this is an allow-list,
    not a filter. It is the only place outside input reaches a file path.
    """
    v = str(value or "default").strip()
    return v if _MEMBER_RE.match(v) else "default"


def atomic_write(path, text):
    """Atomic write: temp file in the same folder, fsync, then os.replace.
    A crash or power cut mid-write can't leave data.json as half a JSON file.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2))


def load_data():
    with _lock:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        # Old single-person layout {targets, actual} -> members list
        if not isinstance(d.get("members"), list) or not d["members"]:
            d = {
                "active": "default",
                "members": [
                    {"id": "default", "name": "Me",
                     "start_weight": d.get("start_weight", 78.0),
                     "targets": d.get("targets", []),
                     "actual": d.get("actual", {})}
                ]
            }
        else:
            for m in d["members"]:
                if isinstance(m, dict):
                    m.setdefault("start_weight", 78.0)
                    m.setdefault("targets", [])
                    m.setdefault("actual", {})
            if not d.get("active"):
                d["active"] = d["members"][0]["id"]
        # rev must always exist: if the client never receives it, it never sends it back
        # and the conflict check on POST is skipped (that's what happens on a fresh data.json).
        if not isinstance(d.get("rev"), int):
            d["rev"] = 0
        return d


def get_member(d, mid=None):
    mid = mid or d.get("active")
    for m in d.get("members", []):
        if m.get("id") == mid:
            return m
    return None


BACKUP_KEEP = 50


def backup_file(src_path, prefix, keep=BACKUP_KEEP):
    """Snapshot a file into backups/ before writing: deduplicated, newest `keep` per prefix.
    Covers data and content, and also diary.json (the user's own words) and attach.json
    (which file in the library belongs to whom) -- both harder to recover than numbers.
    Everything goes through this one function.
    """
    try:
        if not os.path.exists(src_path):
            return
        with open(src_path, "r", encoding="utf-8") as f:
            content = f.read()
        os.makedirs(BACKUP_DIR, exist_ok=True)
        head = prefix + "_"
        files = sorted([f for f in os.listdir(BACKUP_DIR)
                        if f.startswith(head) and f.endswith(".json")])
        if files:
            with open(os.path.join(BACKUP_DIR, files[-1]), "r", encoding="utf-8") as f:
                if f.read() == content:
                    return  # unchanged, skip
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        with open(os.path.join(BACKUP_DIR, "%s%s.json" % (head, ts)), "w",
                  encoding="utf-8") as f:
            f.write(content)
        files = sorted([f for f in os.listdir(BACKUP_DIR)
                        if f.startswith(head) and f.endswith(".json")])
        while len(files) > keep:
            try:
                os.remove(os.path.join(BACKUP_DIR, files.pop(0)))
            except Exception:
                pass
    except Exception:
        pass


def backup_data():
    backup_file(DATA_FILE, "data")


def save_data(d):
    with _lock:
        backup_data()
        # Bump a revision on every write so the page can tell another tab changed the data
        try:
            d["rev"] = int(d.get("rev") or 0) + 1
        except Exception:
            d["rev"] = 1
        write_json(DATA_FILE, d)


def backup_content():
    backup_file(CONTENT_FILE, "content")


def delete_photo(date, meal, member="default"):
    """Move a meal photo into .trash (replace instead of delete; some sandboxes block deletes)."""
    trash_dir = os.path.join(PHOTO_DIR, ".trash")
    os.makedirs(trash_dir, exist_ok=True)
    prefix = "%s_%s_%s" % (member, date, meal)
    for e in PHOTO_EXTS:
        p = os.path.join(PHOTO_DIR, prefix + "." + e)
        if os.path.exists(p):
            os.replace(p, os.path.join(trash_dir, os.path.basename(p)))


def load_photo_meta():
    try:
        with open(PHOTO_META_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_content():
    try:
        with open(CONTENT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_content(d):
    with _lock:
        backup_content()
        write_json(CONTENT_FILE, d)


def save_photo_meta(d):
    with _lock:
        backup_file(PHOTO_META_FILE, "photos", keep=20)
        write_json(PHOTO_META_FILE, d)


def load_diary():
    try:
        with open(DIARY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_diary(d):
    with _lock:
        backup_file(DIARY_FILE, "diary")
        write_json(DIARY_FILE, d)


def load_attach():
    try:
        with open(ATTACH_META, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_attach(d):
    with _lock:
        backup_file(ATTACH_META, "attach")
        write_json(ATTACH_META, d)


def load_llm_config():
    """Model settings (any OpenAI-compatible API) from api_config.json."""
    config = dict(DEFAULT_LLM)
    try:
        with open(API_CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict):
                config.update(d)
    except Exception:
        pass
    return config


def ai_configured():
    """Truthy when AI is set up: a provider URL plus a key. Local servers (Ollama, LM Studio)
    need no key, so a localhost URL counts on its own."""
    cfg = load_llm_config()
    base = (cfg.get("base_url") or "").strip()
    if not base:
        return None
    if cfg.get("api_key"):
        return cfg["api_key"]
    host = urllib.parse.urlparse(base).hostname or ""
    return "local" if host in ("localhost", "127.0.0.1", "::1") else None


def save_llm_config(cfg):
    d = {
        "base_url": (cfg.get("base_url") or "").strip().rstrip("/"),
        "api_key": (cfg.get("api_key") or "").strip(),
        "model": (cfg.get("model") or "").strip(),
        "vision_model": (cfg.get("vision_model") or "").strip(),
        "fast_model": (cfg.get("fast_model") or "").strip(),
    }
    with _lock:
        write_json(API_CONFIG_FILE, d)


def clear_apikey():
    try:
        os.remove(API_KEY_FILE)
    except Exception:
        pass


def clear_llm_config():
    try:
        os.remove(API_CONFIG_FILE)
    except Exception:
        pass
    clear_apikey()


def parse_foods(content):
    """Parse (foods, total, items) out of a model reply."""
    if not content:
        return None

    def _int(v):
        try:
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str):
                m = re.search(r"\d+", v)
                return int(m.group(0)) if m else None
        except Exception:
            pass
        return None

    d = None
    try:
        d = json.loads(content)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            try:
                d = json.loads(m.group(0))
            except Exception:
                d = None
    if d and isinstance(d.get("foods"), list):
        foods = []
        for f in d["foods"]:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "").strip()
            if not name:
                continue
            weight = _int(f.get("weight")) or 0
            cal = _int(f.get("cal") or f.get("calories")) or 0
            foods.append(dict({"name": name, "weight": weight, "cal": cal}, **macro_fields(f)))
        total = _int(d.get("total")) or sum(x["cal"] for x in foods)
        items = ", ".join(x["name"] for x in foods)
        return foods, total, items
    # Fallback: a single calorie number
    m = re.search(r"(\d{2,4})\s*(?:kcal|calories|cal)", content, re.I)
    if m:
        return [], int(m.group(1)), ""
    return None


def recognize_meal(image_path):
    """Identify foods in a photo with the vision model. Returns (foods, total, items)."""
    cfg = load_llm_config()
    if not ai_configured():
        return [], None, None
    model = cfg.get("vision_model") or cfg.get("model") or ""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        prompt = ("Identify the food in this photo. List each item with an estimated weight in grams, calories (kcal), "
                  "and protein, carbs and fat in grams. Use common American portions when unsure. Return JSON only, no other text:"
                  '{"foods":[{"name":"food name","weight":grams,"cal":kcal,"p":protein_g,"c":carbs_g,"f":fat_g}],"total":total_kcal_integer}')
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": _image_url(b64)}},
            {"type": "text", "text": prompt},
        ]}]
        content = _llm_chat(messages, model, timeout=60)
        if not content:
            return [], None, None
        parsed = parse_foods(content)
        if parsed:
            return parsed
    except Exception:
        pass
    return [], None, None


def recognize_food_image(data, ext="jpg"):
    """Identify a food photo for the calorie calculator. The temp file is deleted; it never enters the meal album."""
    if not ai_configured():
        return {"ok": False, "err": "Photo recognition needs AI set up first"}
    ext = str(ext or "jpg").lower().lstrip(".")
    if ext not in PHOTO_EXTS:
        return {"ok": False, "err": "That image format isn't supported"}
    try:
        binary = base64.b64decode(str(data or ""), validate=True)
    except Exception:
        return {"ok": False, "err": "The image data is incomplete"}
    if not binary or len(binary) > 15 * 1024 * 1024:
        return {"ok": False, "err": "Please pick an image under 15 MB"}
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="bodycraft_food_", suffix="." + ext)
        with os.fdopen(fd, "wb") as f:
            f.write(binary)
        foods, total, items = recognize_meal(path)
        clean = []
        for food in foods or []:
            name = str(food.get("name") or "").strip()[:40]
            try:
                weight = max(0, min(5000, int(float(food.get("weight") or 0))))
                cal = max(0, min(5000, int(float(food.get("cal") or 0))))
            except Exception:
                continue
            if name and weight > 0 and cal > 0:
                clean.append({"name": name, "weight": weight, "cal": cal,
                              "category": food_category(name)})
        if not clean:
            return {"ok": False, "err": "Couldn't find any foods and portions in that photo"}
        return {"ok": True, "foods": clean,
                "total": sum(x["cal"] for x in clean), "items": items or ""}
    finally:
        if path:
            try:
                os.remove(path)
            except Exception:
                pass


def coach_image(data, ext, text="", meal_names=None):
    """An image dropped into the coach: first decide whether it's a food photo or a screenshot of text.
    Food photo -> foods, portions, calories, macros, and a guess at which meal.
    Text screenshot (a weight log in Notes, a meal list, a chat) -> transcribe it verbatim and hand it to the
    "log it in a sentence" path. The image only passes through the vision model in memory; the page saves
    the photo to a meal once the user confirms which one."""
    if not ai_configured():
        return {"ok": False, "err": "Reading images needs AI set up first"}
    ext = str(ext or "jpg").lower().lstrip(".")
    if ext not in PHOTO_EXTS:
        return {"ok": False, "err": "That image format isn't supported"}
    try:
        binary = base64.b64decode(str(data or ""), validate=True)
    except Exception:
        return {"ok": False, "err": "The image data is incomplete"}
    if not binary or len(binary) > 15 * 1024 * 1024:
        return {"ok": False, "err": "Please pick an image under 15 MB"}
    cfg = load_llm_config()
    model = cfg.get("vision_model") or cfg.get("model") or ""
    names = meal_names if isinstance(meal_names, dict) and meal_names else \
        dict(MEAL_NAMES)
    prompt = (
        "Look at this image and decide what kind it is. Return JSON only:\n"
        '{"kind":"food"|"text"|"other","meal":"meal key or empty","text":"","foods":[{"name":"food name","weight":grams,"cal":kcal,"p":protein_g,"c":carbs_g,"f":fat_g}],"note":"one sentence"}\n'

        "- Mostly food (a plate, snacks, drinks, takeout) -> kind=food. List each item in foods with estimated grams, calories, protein, carbs and fat. "
        "If it's clear which meal it is, set meal (options: %s); otherwise leave it empty.\n"
        "- Mostly text (a notes app, a chat, a weight log, a nutrition facts label) -> kind=text. "
        "Copy the text into text verbatim and completely (one table row per line; don't drop a single number or date).\n"
        "- Neither -> kind=other, and say in note what the image shows.\n"
        "What the user said with it: %s"
    ) % (", ".join("%s=%s" % (k, v) for k, v in names.items()), (str(text or "").strip()[:300] or "(nothing)"))
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": _image_url(base64.b64encode(binary).decode())}},
        {"type": "text", "text": prompt},
    ]}]
    content = _llm_chat(messages, model, timeout=90, max_tokens=4096, reasoning_ok=True)
    d = _extract_json(content or "", want=("kind", "foods", "text")) or {}
    kind = str(d.get("kind") or "").strip().lower()
    foods = []
    for f in (d.get("foods") or []) if isinstance(d.get("foods"), list) else []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()[:40]
        if not name:
            continue
        try:
            weight = max(0, min(5000, int(float(f.get("weight") or 0))))
            cal = max(0, min(5000, int(float(f.get("cal") or 0))))
        except Exception:
            weight, cal = 0, 0
        foods.append(dict({"name": name, "weight": weight, "cal": cal}, **macro_fields(f)))
    txt = str(d.get("text") or "").strip()[:4000]
    if kind not in ("food", "text", "other"):
        kind = "food" if foods else ("text" if txt else "other")
    if kind == "food" and not foods:
        kind = "text" if txt else "other"
    if kind == "text" and not txt:
        kind = "food" if foods else "other"
    meal = str(d.get("meal") or "").strip()
    if meal not in names:
        meal = ""
    if not content:
        return {"ok": False, "err": "The vision model didn't reply. " + _llm_why()}
    return {"ok": True, "kind": kind, "meal": meal, "text": txt, "foods": foods,
            "total": sum(x["cal"] for x in foods), "items": ", ".join(x["name"] for x in foods),
            "note": str(d.get("note") or "").strip()[:120]}


def _ensure_safety(messages):
    """Prepend the safety clause here, once, instead of trusting every prompt to include it.
    Every model call ends up in _llm_chat; putting the constraint at this single exit means
    no prompt can forget it. Skipped if already present; sent as a system message.
    """
    if not SAFETY_CLAUSE:
        return messages
    def _text(c):
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
        return ""
    for m in messages:
        if SAFETY_CLAUSE.strip()[:20] in _text(m.get("content")):
            return messages
    return [{"role": "system", "content": SAFETY_CLAUSE}] + list(messages)


def _image_url(b64):
    """Image URL for a vision message: providers expect a data:image/...;base64, URL."""
    b64 = str(b64 or "")
    if b64.startswith("data:"):
        return b64
    head = b64[:8]
    mime = ("image/png" if head.startswith("iVBOR") else "image/gif" if head.startswith("R0lG")
            else "image/webp" if head.startswith("UklG") else "image/jpeg")
    return "data:%s;base64,%s" % (mime, b64)


def _is_claude(base):
    return "anthropic.com" in (base or "")


def _llm_chat(messages, model, temperature=0.3, timeout=90, max_tokens=4096, reasoning_ok=False):
    """One call to an OpenAI-compatible /chat/completions endpoint. Returns the text, or None.

    Providers disagree on a few parameters, so the call adapts instead of failing:
    - newer OpenAI and Claude models reject temperature != 1  -> we don't send temperature;
    - OpenAI reasoning models reject max_tokens and want max_completion_tokens -> on that 400,
      retry once with the other name and remember it for this provider.
    max_tokens is always sent explicitly: many providers default to ~1024, and a truncated JSON
    reply looks to the user like "couldn't understand you", however they rephrase.
    """
    cfg = load_llm_config()
    key = cfg.get("api_key") or ""
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base or not ai_configured():
        return None
    url = base + "/chat/completions"
    messages = _ensure_safety(messages)
    tok_name = _PARAM_FIX.get(base, "max_tokens")
    payload = {"model": model, "messages": messages, tok_name: int(max_tokens)}
    cap = _MAX_TOKENS_CAP.get((base, model))
    if cap and int(max_tokens) > cap:
        payload[tok_name] = cap
    if _is_claude(base):
        # Claude 5 thinks by default; for our short structured jobs that only burns tokens.
        payload["thinking"] = {"type": "disabled"}

    def _post(pl):
        headers = {"Content-Type": "application/json"}
        if key and key != "local":
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(url, data=json.dumps(pl).encode("utf-8"), headers=headers)
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read().decode("utf-8"))

    try:
        body = None
        for _attempt in range(4):
            try:
                body = _post(payload)
                break
            except urllib.error.HTTPError as he:
                if he.code != 400:
                    raise
                detail = ""
                try:
                    detail = he.read().decode("utf-8", "replace")
                except Exception:
                    pass
                low = detail.lower()
                cap = _cap_from_error(detail)
                tk = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
                if cap and int(payload.get(tk) or 0) > cap:
                    payload[tk] = cap
                    _MAX_TOKENS_CAP[(base, model)] = cap
                    continue
                if "max_tokens" in low and "max_completion_tokens" in low and "max_tokens" in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    _PARAM_FIX[base] = "max_completion_tokens"
                    continue
                if "thinking" in payload and "thinking" in low:
                    payload.pop("thinking", None)
                    continue
                raise urllib.error.HTTPError(he.url, 400, detail[:300], he.headers, None)
        if body is None:
            return None
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        # Reasoning models may put everything in reasoning_content and leave content empty.
        # Callers that want JSON can accept that text (reasoning_ok=True) and dig the JSON out.
        # Callers that show text to the user must not: a model's scratchpad pasted in as a
        # weekly review is worse than saying "no reply".
        if not (content or "").strip():
            content = (msg.get("reasoning_content") or msg.get("reasoning") or "") if reasoning_ok else ""
        _llm_note(model, choice.get("finish_reason"), content)
        return content
    except urllib.error.HTTPError as e:
        detail = str(e.reason or "")
        try:
            detail = detail or e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        _llm_note(model, "http %s" % e.code, "", "HTTP %s %s" % (e.code, detail[:300]))
        return None
    except Exception as e:
        _llm_note(model, "error", "", "%s: %s" % (type(e).__name__, e))
        return None


# Parameter names a provider turned out to want, keyed by base_url (this run only).
_PARAM_FIX = {}


# Some providers cap max_tokens per model (Zhipu's vision models: 2048) and answer a bigger value
# with a 400 like "max_tokens ... range [1,2048]". We read the limit from that message, retry once
# with it, and remember it per (base_url, model) for this run.
_MAX_TOKENS_CAP = {}


def _cap_from_error(detail):
    low = (detail or "").lower()
    if "max_tokens" not in low and "max_completion_tokens" not in low:
        return None
    m = (re.search(r"\[\s*\d+\s*,\s*(\d{2,6})\s*\]", detail)
         or re.search(r"(?:at most|maximum(?: value)?(?: is| of)?|less than or equal to|<=|\u4e0d\u8d85\u8fc7|\u6700\u5927(?:\u503c)?(?:\u4e3a|\u662f)?)\s*:?\s*(\d{2,6})", detail, re.I))
    return int(m.group(1)) if m else None

# Summaries of the last few model calls, in memory only -- for working out why a model misbehaved.
_LLM_TRACE = []


def _llm_note(model, finish_reason, content, err=""):
    with _lock:
        _LLM_TRACE.append({"at": datetime.datetime.now().strftime("%H:%M:%S"),
                           "model": model, "finish": finish_reason,
                           "len": len(content or ""), "err": err,
                           "head": (content or "")[:200]})
        del _LLM_TRACE[:-12]


def _llm_why():
    """Turn the last call's actual outcome into one sentence the user can act on.
    Says what really happened (HTTP code and the provider's own words, or an empty reply)
    rather than guessing -- a wrong guess sends people changing the wrong setting.
    """
    with _lock:
        last = _LLM_TRACE[-1] if _LLM_TRACE else None
    if not last:
        return "No reply from the model. Try again in a moment."
    err = last.get("err") or ""
    fin = str(last.get("finish") or "")
    if err.startswith("HTTP 401") or err.startswith("HTTP 403"):
        return "The provider rejected the API key (%s). Check it under AI settings." % err[:60]
    if err.startswith("HTTP 429"):
        return "The provider says you're sending too many requests or are out of credit. Try again in a few minutes."
    if err.startswith("HTTP 400"):
        return "The provider rejected the request: %s. Usually the model name under AI settings is wrong." % err[:160]
    if err.startswith("HTTP"):
        return "The provider returned an error: %s" % err[:160]
    if "timed out" in err or "timeout" in err.lower():
        return "The model took too long to answer. Try a faster model under AI settings."
    if err:
        return "Couldn't reach the provider: %s" % err[:160]
    if fin == "length":
        return ("The model ran out of room before writing an answer -- it spent it all on reasoning. "
                "Under AI settings, set a small non-reasoning model for logging (for example gpt-5-nano, claude-haiku-5-5 or gemini-2.5-flash-lite).")
    return ("The model replied with an empty answer (finish_reason=%s). "
            "A smaller, non-reasoning model usually fixes this." % (fin or "unknown"))


def llm_trace_dump(tag, *raws):
    """When a model keeps replying in the wrong format, append its raw replies to .llm_debug.txt."""
    try:
        with _lock:
            with open(os.path.join(BASE, ".llm_debug.txt"), "a", encoding="utf-8") as f:
                f.write("\n===== %s %s =====\n" % (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tag))
                for i, raw in enumerate(raws):
                    f.write("--- attempt %d ---\n%s\n" % (i + 1, (raw or "(empty)")[:12000]))
    except Exception:
        pass


LOOKUP_FILE = os.path.join(BASE, "food_lookup.json")


def load_lookup():
    """My Foods: calories per unit. Look something up once and it's remembered -- no AI needed next time."""
    try:
        with open(LOOKUP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_lookup(d):
    with _lock:
        write_json(LOOKUP_FILE, d)


# Starter food list: everyday foods people log most, written into food_lookup.json on first run.
# Values are rounded from USDA FoodData Central -- good enough to log with, not lab-exact.
# Anything the user looks up or edits overrides an entry; deleted entries don't come back.
# "per" is calories per unit: per gram/ml for g/ml, per item for each/slice/cup/tbsp etc.
FOOD_SEED = [
    # (name, unit, kcal per unit, note, protein g, carbs g, fat g -- all per unit)
    # Grains & starches
    ("White rice", "g", 1.30, "cooked; 1 cup ≈ 158 g", 0.027, 0.28, 0.003),
    ("Brown rice", "g", 1.12, "cooked; 1 cup ≈ 195 g", 0.026, 0.23, 0.009),
    ("Quinoa", "g", 1.20, "cooked; 1 cup ≈ 185 g", 0.044, 0.21, 0.019),
    ("Rolled oats", "g", 3.79, "dry; ½ cup ≈ 40 g", 0.13, 0.68, 0.065),
    ("Pasta", "g", 1.58, "cooked; 1 cup ≈ 140 g", 0.058, 0.31, 0.009),
    ("Bagel", "g", 2.57, "plain; a medium bagel ≈ 105 g", 0.10, 0.50, 0.016),
    ("Whole wheat bread", "slice", 80, "about 32 g", 4.0, 14.0, 1.1),
    ("White bread", "slice", 75, "about 28 g", 2.5, 14.0, 1.0),
    ("English muffin", "each", 130, "", 4.5, 25.0, 1.0),
    ("Flour tortilla", "each", 140, '8 inch', 3.7, 24.0, 3.5),
    ("Granola", "g", 4.70, "¼ cup ≈ 30 g", 0.10, 0.64, 0.20),
    ("Potato", "g", 0.93, "baked, with skin", 0.025, 0.21, 0.001),
    ("Sweet potato", "g", 0.90, "baked", 0.020, 0.21, 0.002),
    ("Corn on the cob", "each", 90, "medium ear", 3.3, 19.0, 1.4),
    ("Black beans", "g", 1.32, "cooked or canned, drained", 0.089, 0.24, 0.005),
    # Protein
    ("Egg", "each", 72, "large", 6.3, 0.4, 5.0),
    ("Egg white", "each", 17, "from a large egg", 3.6, 0.2, 0.1),
    ("Chicken breast", "g", 1.65, "cooked, skinless; a palm ≈ 100 g", 0.31, 0.0, 0.036),
    ("Chicken thigh", "g", 1.79, "cooked, skinless", 0.25, 0.0, 0.082),
    ("Ground beef 90% lean", "g", 2.17, "cooked", 0.26, 0.0, 0.12),
    ("Sirloin steak", "g", 1.83, "cooked, lean", 0.30, 0.0, 0.065),
    ("Turkey breast deli", "slice", 30, "about 28 g", 5.0, 1.0, 0.5),
    ("Bacon", "slice", 43, "cooked", 3.0, 0.1, 3.3),
    ("Salmon", "g", 2.06, "cooked", 0.22, 0.0, 0.12),
    ("Tuna", "g", 1.16, "canned in water, drained", 0.26, 0.0, 0.008),
    ("Shrimp", "g", 0.99, "cooked", 0.24, 0.002, 0.003),
    ("Tofu", "g", 1.44, "firm", 0.17, 0.03, 0.087),
    ("Edamame", "g", 1.21, "shelled", 0.12, 0.089, 0.052),
    ("Greek yogurt", "g", 0.59, "plain nonfat; a 5.3 oz cup ≈ 150 g", 0.10, 0.036, 0.004),
    ("Cottage cheese", "g", 0.81, "2%; ½ cup ≈ 113 g", 0.104, 0.047, 0.023),
    ("Milk 2%", "ml", 0.50, "1 cup ≈ 240 ml", 0.033, 0.048, 0.019),
    ("Skim milk", "ml", 0.34, "1 cup ≈ 240 ml", 0.034, 0.050, 0.001),
    ("Almond milk", "ml", 0.13, "unsweetened", 0.004, 0.003, 0.011),
    ("Whey protein", "scoop", 120, "about 30 g", 24.0, 3.0, 1.5),
    ("Protein bar", "bar", 200, "typical 60 g bar", 20.0, 22.0, 7.0),
    ("American cheese", "slice", 65, "about 19 g", 3.5, 1.5, 5.0),
    ("Cheddar", "g", 4.03, "1 oz ≈ 28 g", 0.25, 0.013, 0.33),
    ("String cheese", "each", 80, "", 7.0, 1.0, 6.0),
    # Vegetables
    ("Broccoli", "g", 0.34, "", 0.028, 0.07, 0.004),
    ("Spinach", "g", 0.23, "", 0.029, 0.036, 0.004),
    ("Romaine lettuce", "g", 0.17, "", 0.012, 0.033, 0.003),
    ("Tomato", "g", 0.18, "a large tomato ≈ 180 g", 0.009, 0.039, 0.002),
    ("Cucumber", "g", 0.15, "", 0.007, 0.036, 0.001),
    ("Carrots", "g", 0.41, "", 0.009, 0.096, 0.002),
    ("Bell pepper", "g", 0.31, "", 0.010, 0.060, 0.003),
    ("Zucchini", "g", 0.17, "", 0.012, 0.031, 0.003),
    ("Mushrooms", "g", 0.22, "", 0.031, 0.033, 0.003),
    ("Green beans", "g", 0.31, "", 0.018, 0.070, 0.002),
    ("Asparagus", "g", 0.20, "", 0.022, 0.039, 0.001),
    ("Cauliflower", "g", 0.25, "", 0.019, 0.050, 0.003),
    # Fruit
    ("Apple", "each", 95, "medium, about 180 g", 0.5, 25.0, 0.3),
    ("Banana", "each", 105, "medium, about 118 g", 1.3, 27.0, 0.4),
    ("Orange", "each", 62, "medium", 1.2, 15.4, 0.2),
    ("Blueberries", "g", 0.57, "1 cup ≈ 148 g", 0.007, 0.145, 0.003),
    ("Strawberries", "g", 0.32, "1 cup ≈ 150 g", 0.007, 0.077, 0.003),
    ("Grapes", "g", 0.69, "", 0.007, 0.18, 0.002),
    ("Avocado", "each", 240, "medium; half ≈ 120", 3.0, 12.8, 22.0),
    # Nuts, spreads & fats
    ("Almonds", "g", 5.79, "1 oz ≈ 23 almonds ≈ 28 g", 0.21, 0.22, 0.50),
    ("Walnuts", "g", 6.54, "", 0.15, 0.14, 0.65),
    ("Peanut butter", "tbsp", 95, "", 3.5, 3.5, 8.0),
    ("Hummus", "tbsp", 25, "", 1.2, 2.1, 1.4),
    ("Olive oil", "tbsp", 119, "", 0.0, 0.0, 13.5),
    ("Butter", "tbsp", 102, "", 0.1, 0.0, 11.5),
    ("Mayonnaise", "tbsp", 94, "", 0.1, 0.1, 10.3),
    # Snacks & mixed
    ("Dark chocolate", "g", 5.98, "70–85%", 0.078, 0.46, 0.43),
    ("Potato chips", "g", 5.36, "a 1 oz bag ≈ 28 g", 0.07, 0.53, 0.34),
    ("Popcorn", "cup", 31, "air-popped", 1.0, 6.2, 0.4),
    ("Cheese pizza", "slice", 285, "14 inch pie", 12.0, 36.0, 10.0),
    # Drinks
    ("Black coffee", "cup", 2, "", 0.3, 0.0, 0.0),
    ("Latte", "each", 150, "12 oz with 2% milk", 10.0, 15.0, 6.0),
    ("Orange juice", "ml", 0.45, "1 cup ≈ 240 ml", 0.007, 0.104, 0.002),
    ("Soda", "can", 140, "12 oz", 0.0, 39.0, 0.0),
    ("Beer", "can", 153, "12 oz regular", 1.6, 13.0, 0.0),
    ("Light beer", "can", 103, "12 oz", 0.9, 6.0, 0.0),
    ("Wine", "glass", 125, "5 oz", 0.1, 4.0, 0.0),
]

FOOD_CATEGORY_NAMES = {
    "Carbs": {"White rice", "Brown rice", "Quinoa", "Rolled oats", "Pasta", "Bagel", "Whole wheat bread",
              "White bread", "English muffin", "Flour tortilla", "Granola", "Potato", "Sweet potato",
              "Corn on the cob", "Black beans"},
    "Protein": {"Egg", "Egg white", "Chicken breast", "Chicken thigh", "Ground beef 90% lean", "Sirloin steak",
                "Turkey breast deli", "Bacon", "Salmon", "Tuna", "Shrimp", "Tofu", "Edamame", "Greek yogurt",
                "Cottage cheese", "Milk 2%", "Skim milk", "Whey protein", "Protein bar", "American cheese",
                "Cheddar", "String cheese"},
    "Veggies": {"Broccoli", "Spinach", "Romaine lettuce", "Tomato", "Cucumber", "Carrots", "Bell pepper",
                "Zucchini", "Mushrooms", "Green beans", "Asparagus", "Cauliflower"},
    "Fruit": {"Apple", "Banana", "Orange", "Blueberries", "Strawberries", "Grapes", "Avocado"},
}
FOOD_CATEGORIES = ("Carbs", "Protein", "Veggies", "Fruit", "Other")


def food_category(name, supplied=None):
    """Stable category for the My Foods list; works for old entries without a category too."""
    if supplied in FOOD_CATEGORIES:
        return supplied
    name = str(name or "").strip()
    for category, names in FOOD_CATEGORY_NAMES.items():
        if name in names:
            return category
    low = name.lower()
    rules = (
        ("Fruit", ("apple", "banana", "orange", "berr", "peach", "pear", "grape", "melon", "mango", "pineapple", "cherr", "kiwi", "plum")),
        ("Veggies", ("lettuce", "spinach", "kale", "broccoli", "cabbage", "carrot", "tomato", "cucumber", "pepper",
                     "zucchini", "squash", "mushroom", "bean sprout", "asparagus", "cauliflower", "celery", "onion", "salad", "greens")),
        ("Protein", ("chicken", "turkey", "beef", "steak", "pork", "ham", "lamb", "fish", "salmon", "tuna", "cod", "shrimp",
                     "egg", "tofu", "tempeh", "yogurt", "cottage", "whey", "protein", "milk", "cheese", "jerky")),
        ("Carbs", ("rice", "bread", "bagel", "toast", "pasta", "noodle", "oat", "cereal", "potato", "tortilla", "wrap",
                   "quinoa", "muffin", "pancake", "waffle", "corn", "bean", "lentil", "cracker")),
    )
    for category, words in rules:
        if any(word in low for word in words):
            return category
    return "Other"


def seed_lookup_if_empty():
    """Write the starter list if My Foods is empty. First run only; deleted entries don't come back."""
    try:
        if os.path.exists(LOOKUP_FILE):
            return
        table = {}
        for name, unit, per, note, p, c, f in FOOD_SEED:
            table[lookup_key(name, unit)] = {"per": per, "name": name, "unit": unit,
                                             "note": note, "category": food_category(name), "seed": True,
                                             "p": p, "c": c, "f": f}
        save_lookup(table)
    except Exception:
        pass


def backfill_seed_macros():
    """Fill in protein/carbs/fat for starter entries saved before macros existed.
    Entries the user looked up or edited (no "seed" flag) are left alone."""
    try:
        table = load_lookup()
        if not table:
            return
        seed = {lookup_key(x[0], x[1]): x for x in FOOD_SEED}
        changed = False
        for k, rec in table.items():
            if not isinstance(rec, dict) or not rec.get("seed") or "p" in rec:
                continue
            x = seed.get(k)
            if x:
                rec["p"], rec["c"], rec["f"] = x[4], x[5], x[6]
                changed = True
        if changed:
            save_lookup(table)
    except Exception:
        pass


# Macros: protein, carbs and fat per food, in grams. Clamped to sane values;
# missing ones (old records, calories-only entries) stay empty -- shown as "—", never faked as 0.
def _macro(v):
    try:
        x = float(v)
    except Exception:
        return None
    if x != x or x < 0:
        return None
    return round(min(500.0, x), 1)


def macro_fields(src):
    """p/c/f from a model or table record; all three or nothing."""
    out = {}
    for k, alts in (("p", ("p", "protein")), ("c", ("c", "carb", "carbs")), ("f", ("f", "fat"))):
        v = None
        for a in alts:
            if isinstance(src, dict) and src.get(a) is not None:
                v = _macro(src.get(a))
                break
        if v is None:
            return {}
        out[k] = v
    return out


def macro_raw(rec):
    """Per-unit p/c/f as stored in My Foods (per-gram values like 0.027 must not be rounded to 0.0)."""
    if not isinstance(rec, dict):
        return {}
    out = {}
    for k in ("p", "c", "f"):
        try:
            v = float(rec.get(k))
        except Exception:
            return {}
        if v != v or v < 0:
            return {}
        out[k] = round(v, 4)
    return out


def scale_macros(rec, amt):
    """Per-unit p/c/f from My Foods x amount."""
    if not isinstance(rec, dict) or rec.get("p") is None:
        return {}
    try:
        return {k: round(float(rec[k]) * amt, 1) for k in ("p", "c", "f")}
    except Exception:
        return {}


COOK_WORDS = ("grilled", "baked", "roasted", "roast", "steamed", "boiled", "hard-boiled", "soft-boiled",
              "fried", "pan-fried", "air-fried", "pan-seared", "seared", "sauteed", "sautéed", "poached",
              "scrambled", "raw", "cooked", "fresh", "plain", "skinless", "boneless", "lean", "sliced",
              "diced", "chopped", "shredded", "toasted", "homemade", "organic", "large", "medium", "small")
PIECE_UNITS = ("each", "slice", "cup", "tbsp", "tsp", "scoop", "bar", "can", "glass", "serving", "oz")


def _norm_food(s):
    s = re.sub(r"[^\w%\- ]+", " ", str(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _only_cook_words(rest):
    """Is what's left made only of cooking words ("grilled", "skinless"...)? Empty counts."""
    words = [w for w in _norm_food(rest).split(" ") if w]
    return all(w in COOK_WORDS for w in words)


def lookup_match(table, name, unit, unit_auto=False):
    """Find a food in My Foods. Returns (record, unit actually used) or (None, None).

    Exact "name|unit" matching misses the two everyday cases:
      - different unit: the list has "Egg|each", the meal form defaults to grams
      - slightly different name: "grilled chicken breast" vs "Chicken breast"
    and then a food the user already has gets sent to the AI again, with a different answer each time.

    Order: exact -> same name in any unit -> containment where the extra words are only cooking words.
    "Chicken parmesan" does NOT match "Chicken": the leftover word changes the calories, so asking the
    AI beats a confident wrong number. Case-insensitive.
    Unit mismatch: if the user never touched the unit (unit_auto), use the list's unit; if they chose one,
    respect it and only match the same kind (weight/volume vs pieces).
    """
    name = str(name or "").strip()
    unit = str(unit or "g").strip()
    if not name:
        return None, None
    rec = table.get(lookup_key(name, unit))
    if rec and rec.get("per"):
        return rec, unit
    want = _norm_food(name)
    best = None       # (score, rec, unit)
    for k, rec in table.items():
        if not rec or not rec.get("per"):
            continue
        tname = _norm_food(rec.get("name") or k.split("|")[0])
        tunit = str(rec.get("unit") or k.split("|")[-1]).strip()
        if not tname:
            continue
        score = 0
        if tname == want:
            score = 100
        elif re.search(r"(^| )%s( |$)" % re.escape(tname), want):
            if _only_cook_words(want.replace(tname, " ")):
                score = 80 + len(tname)
        elif len(want) >= 3 and re.search(r"(^| )%s( |$)" % re.escape(want), tname):
            # Short form typed: "chicken breast" -> "Chicken breast skinless". Prefer the shortest entry.
            score = 60 + max(0, 30 - len(tname))
        if not score:
            continue
        same_kind = (unit in ("g", "ml", "oz")) == (tunit in ("g", "ml", "oz"))
        if tunit == unit:
            score += 10
        elif same_kind:
            score += 5
        elif not unit_auto:
            continue
        if best is None or score > best[0]:
            best = (score, rec, tunit)
    if best:
        return best[1], best[2]
    return None, None


def lookup_key(name, unit):
    return "%s|%s" % (str(name or "").strip()[:40], str(unit or "g").strip()[:8])


FAV_FILE = os.path.join(BASE, "favorites.json")


class _TextGrab(html.parser.HTMLParser):
    """Pull the readable text out of a web page. Standard library only."""

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "svg", "noscript", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.depth_skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth_skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("br", "p", "li", "div", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth_skip > 0:
            self.depth_skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()[:80]
        if self.depth_skip:
            return
        t = data.strip()
        if t:
            self.parts.append(t)

    def text(self):
        raw = " ".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"(\s*\n\s*)+", "\n", raw)
        return raw.strip()


def check_fetch_url(url):
    """Decide whether a URL may be fetched. Returns (ok, reason).
    This is the only place the app makes a request to an address the user supplied, so the
    limits apply before the request, not after:
      - http/https only (no file:// and friends)
      - any private/internal IP is refused, or a link on a page could make this machine
        talk to your router or other devices on your network
      - redirects aren't followed (the target could dodge the checks above)
    """
    try:
        u = urllib.parse.urlparse(str(url or "").strip())
    except Exception:
        return False, "That doesn't look like a web address"
    if u.scheme not in ("http", "https"):
        return False, "Only http:// and https:// addresses are supported"
    host = (u.hostname or "").strip()
    if not host:
        return False, "There's no domain in that address"
    if host.lower() in ("localhost", "localhost.localdomain"):
        return False, "Can't fetch addresses on this computer"
    try:
        import socket as _s
        infos = _s.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                               proto=_s.IPPROTO_TCP)
    except Exception:
        return False, "That domain couldn't be resolved"
    for info in infos:
        ip = info[4][0]
        if is_private(ip) or is_loopback(ip):
            return False, "That address points to a private network; not fetching it for safety"
    return True, ""


def fetch_recipe(url, member="default"):
    """Fetch a web page and have the model pull the recipe out of it."""
    ok, why = check_fetch_url(url)
    if not ok:
        return {"ok": False, "err": why}
    if not ai_configured():
        return {"ok": False, "err": "Importing recipes needs AI set up first"}

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        # Don't follow redirects: the target could get around the address check above
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; Bodycraft/1.0)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        resp = opener.open(req, timeout=15)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return {"ok": False, "err": "That address isn't a web page (%s)" % (ctype.split(";")[0] or "unknown")}
        raw = resp.read(3 * 1024 * 1024)      # read at most 3 MB
        enc = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype)
        if m:
            enc = m.group(1)
        html_text = raw.decode(enc, "ignore")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return {"ok": False, "err": "That address redirects, and redirects aren't followed for safety. Try the address it redirects to."}
        return {"ok": False, "err": "Couldn't open that page (HTTP %s)" % e.code}
    except Exception as e:
        return {"ok": False, "err": "Couldn't open that page: %s" % str(e)[:60]}

    g = _TextGrab()
    try:
        g.feed(html_text)
    except Exception:
        pass
    body = g.text()
    if len(body) < 60:
        return {"ok": False, "err": "Found almost no text on that page -- it may need a login or be rendered by scripts"}
    body = body[:20000]

    prompt = (
        SAFETY_CLAUSE
        + knowledge_text(member)
        + "Below is the text of a web page. Extract the recipe from it, completely.\n\n"
        + ("Page title: %s\n" % g.title if g.title else "")
        + "Page text:\n%s\n\n" % body
        + "Return JSON only, no explanation:\n"
        '{"found": true, "title":"dish name (under 40 characters)", "emoji":"🍲", '

        '"servings": how many servings the recipe makes (integer), '

        '"ingredients":[{"name":"ingredient","amount":"quantity, e.g. 1 cup / 200 g / 2","cal":calories for that amount (integer)}], '

        '"steps":["step one","step two"], '

        '"total_cal": calories for the whole recipe (integer), "per_serving_cal": calories per serving (integer), '

        '"minutes": approximate total minutes (integer), "tags":["tag"], "slot":"breakfast|lunch|dinner|null"}\n'

        "Rules:\n"
        '- If the page has no recipe, return {"found": false}\n'

        "- servings: as stated on the page; if not stated, estimate from the quantities (e.g. 12 muffins = 12 servings)\n"
        "- ingredients: list them all, each with its amount and the calories for that amount; salt, pepper and spices can be one item\n"
        "- steps: in the page's order, complete, one sentence each, followable; don't skip any\n"
        "- total_cal is the sum of the ingredients; per_serving_cal = total_cal / servings. Give both, and make them agree\n"
        "- tags: one to three short tags, like \"baking\", \"20 min\", \"vegetarian\"\n"
        "- plain English, no exclamation marks or marketing tone"
    )
    d = _extract_json(_call_llm_text(prompt, max_tokens=3000, fast=True) or "")
    if not isinstance(d, dict) or not d.get("found"):
        return {"ok": False, "err": "Couldn't find a recipe on that page"}
    title = str(d.get("title") or "").strip()[:48]
    if not title:
        return {"ok": False, "err": "Couldn't tell what the dish is called"}
    def _int(v, lo=0, hi=99999):
        try:
            n = int(float(v))
            return n if lo <= n <= hi else 0
        except Exception:
            return 0
    servings = _int(d.get("servings"), 1, 100) or 1
    ingredients = []
    for it in (d.get("ingredients") or [])[:30]:
        if isinstance(it, dict) and str(it.get("name") or "").strip():
            ingredients.append({"name": str(it["name"]).strip()[:40],
                                "amount": str(it.get("amount") or "").strip()[:24],
                                "cal": _int(it.get("cal"), 0, 9000)})
    steps = [str(x).strip()[:200] for x in (d.get("steps") or [])[:30] if str(x).strip()]
    total = _int(d.get("total_cal"), 0, 50000)
    per = _int(d.get("per_serving_cal"), 0, 9000)
    # Keep it consistent: ingredient calories sum to the total; total / servings gives per serving
    ing_sum = sum(x["cal"] for x in ingredients)
    if ing_sum > 0:
        total = ing_sum
    if total > 0 and (not per or abs(per * servings - total) > total * 0.3):
        per = int(round(total / servings))
    slot = d.get("slot") if d.get("slot") in ("breakfast", "lunch", "dinner") else None
    return {"ok": True, "item": {
        "title": title,
        "emoji": str(d.get("emoji") or "🍲")[:4],
        "servings": servings,
        "cal": per,                      # the calories on a saved card are always per serving
        "total_cal": total,
        "items": [{"name": x["name"], "amount": x["amount"], "cal": x["cal"]} for x in ingredients],
        "steps": steps,
        "minutes": _int(d.get("minutes"), 0, 1440),
        "desc": " ".join(steps)[:300] if steps else str(d.get("desc") or "").strip()[:300],
        "tags": [str(t)[:16] for t in (d.get("tags") or []) if str(t).strip()][:3],
        "slot": slot,
        "source": url[:200],
    }}


def wipe_records():
    """Clear the record files, after an automatic backup. Same as the first option in reset_data.py."""
    import shutil
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BASE, "backups", "before-reset_" + stamp)
    files = ["data.json", "photos.json", "diary.json", "selfies.json", "attach.json"]
    dirs = ["photos", "selfies", "files"]
    # The fresh data.json takes its start weight from content.json. A hard-coded number would
    # disagree with the settings and the app would think you'd lost (or gained) the difference,
    # unlocking achievements for weight you never lost.
    cur = load_content()
    prof = cur.get("profile") if isinstance(cur.get("profile"), dict) else {}
    keep_name = "Me"
    try:
        old_data = load_data()
        for m in (old_data.get("members") or []):
            if m.get("id") == "default" and m.get("name"):
                keep_name = m["name"]
                break
    except Exception:
        pass
    sw = prof.get("start_weight")
    try:
        sw = float(sw)
        if not (30 <= sw <= 250):
            sw = 70.0
    except Exception:
        sw = 70.0
    fresh = {
        "data.json": {"active": "default",
                      "members": [{"id": "default", "name": keep_name, "start_weight": sw,
                                   "targets": [], "actual": {}}]},
        "photos.json": {}, "diary.json": {}, "selfies.json": {}, "attach.json": {},
    }
    removed = 0
    try:
        os.makedirs(dest, exist_ok=True)
        for f in files:
            src = os.path.join(BASE, f)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dest, f))
        for d in dirs:
            src = os.path.join(BASE, d)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(dest, d), dirs_exist_ok=True)
    except Exception:
        pass
    with _lock:
        for f in files:
            pth = os.path.join(BASE, f)
            if os.path.exists(pth):
                os.remove(pth)
                removed += 1
            if f in fresh:
                try:
                    with open(pth, "w", encoding="utf-8") as fh:
                        json.dump(fresh[f], fh, ensure_ascii=False, indent=2)
                except Exception:
                    pass
        for d in dirs:
            pth = os.path.join(BASE, d)
            if os.path.isdir(pth):
                shutil.rmtree(pth, ignore_errors=True)
                removed += 1
        for f in (".rollback.json",):
            pth = os.path.join(BASE, f)
            if os.path.exists(pth):
                os.remove(pth)
    return {"ok": True, "removed": removed,
            "backup": os.path.relpath(dest, BASE)}


def food_lookup_batch(names, member="default"):
    """Look up several foods at once, no amounts needed -- each gets a typical serving.
    Checks My Foods first and sends only the unknown ones to the AI, together (one request, not N).
    The model gives each a normal serving (one egg, a cup of rice, a slice of toast) with calories,
    and the answers are saved back to My Foods.
    """
    clean = []
    seen = set()
    for n in (names or [])[:20]:
        n = str(n or "").strip()[:40]
        if n and n not in seen:
            seen.add(n)
            clean.append(n)
    if not clean:
        return {"ok": False, "err": "Type a few food names"}
    table = load_lookup()
    out = []
    ask = []
    for n in clean:
        hit = None
        # My Foods is keyed "name|unit": any unit counts; prefer serving-like units over grams
        for unit in ("each", "slice", "cup", "tbsp", "scoop", "bar", "can", "glass", "serving", "tsp", "oz", "g", "ml"):
            rec = table.get(lookup_key(n, unit))
            if rec and rec.get("per"):
                hit = (unit, rec)
                break
        if hit:
            unit, rec = hit
            amt = 100 if unit in ("g", "ml") else 1
            out.append(dict({"name": n, "unit": unit, "amount": amt,
                             "cal": int(round(rec["per"] * amt)), "per": rec["per"],
                             "note": rec.get("note") or "", "from": "local"}, **scale_macros(rec, amt)))
        else:
            ask.append(n)
    if ask:
        if not ai_configured():
            for n in ask:
                out.append({"name": n, "unit": "g", "amount": 0, "cal": 0, "per": 0,
                            "note": "Not in My Foods yet; looking it up needs AI", "from": "none"})
        else:
            prompt = (
                knowledge_text(member)
                + "For each food below, give one typical everyday serving and its calories. Return a JSON array only, no explanation:\n"
                + "\n".join("- " + n for n in ask)
                + "\n\nFormat: [{\"name\":\"food name exactly as given\",\"amount\":quantity,\"unit\":\"unit\","
                  "\"cal\":calories for that serving (integer),\"per100\":calories per 100 g or 100 ml (integer),"
                  "\"p\":protein g for that serving,\"c\":carbs g,\"f\":fat g,\"note\":\"one short note\"}]\n"
                "Rules:\n"
                "- unit: an everyday unit -- each, slice, cup, tbsp, scoop, bar, can, glass, serving -- or g / ml. Eggs in \"each\", rice in \"cup\", toast in \"slice\"; for meats use g with amount 100\n"
                "- amount: a normal serving in that unit, e.g. egg 1, rice 1 (a cup is about 158 g cooked), toast 1\n"
                "- cal is for that amount; per100 is per 100 g/ml; they must agree\n"
                "- note: a few words, like \"cooked\" or \"1 cup ≈ 158 g\", under 30 characters\n"
                "- assume typical American home cooking and cooked weights"
            )
            raw = _call_llm_text(prompt, max_tokens=2048, fast=True) or ""
            # We want an array here; _extract_json only returns objects (it would cut an array down
            # to its first element). Strip code fences and try the array first, then look inside an object.
            arr = None
            txt = raw.strip()
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", txt)
            if m:
                txt = m.group(1).strip()
            st, en = txt.find("["), txt.rfind("]")
            if st >= 0 and en > st:
                try:
                    cand = json.loads(txt[st:en + 1])
                    if isinstance(cand, list):
                        arr = cand
                except Exception:
                    pass
            if arr is None:
                d = _extract_json(raw)
                if isinstance(d, dict):
                    for k in ("foods", "items", "list", "data"):
                        if isinstance(d.get(k), list):
                            arr = d[k]
                            break
            got = {}
            for x in (arr or []):
                if not isinstance(x, dict):
                    continue
                nm = str(x.get("name") or "").strip()[:40]
                if not nm:
                    continue
                try:
                    amt = float(x.get("amount") or 1)
                    cal = int(float(x.get("cal") or 0))
                    per100 = float(x.get("per100") or 0)
                except Exception:
                    continue
                unit = str(x.get("unit") or "g").strip()[:8]
                if cal <= 0 or cal > 5000:
                    continue
                if unit in ("g", "ml"):
                    per = (per100 / 100.0) if per100 > 0 else (cal / amt if amt > 0 else 0)
                    amt = amt if amt > 0 else 100
                else:
                    per = cal / amt if amt > 0 else cal
                if per <= 0:
                    continue
                note = str(x.get("note") or "").strip()[:30]
                mac = macro_fields(x)
                got[nm] = dict({"unit": unit, "amount": int(amt) if amt == int(amt) else round(amt, 1),
                                "cal": cal, "per": round(per, 3), "note": note}, **mac)
                entry = {"per": round(per, 3), "name": nm, "unit": unit,
                         "note": note, "category": food_category(nm)}
                if mac and amt > 0:
                    entry.update({k: round(v / amt, 4) for k, v in mac.items()})
                table[lookup_key(nm, unit)] = entry
            if got:
                save_lookup(table)
            for n in ask:
                g = got.get(n)
                if not g:
                    # the model may have reworded the name; try a loose match
                    for k, v in got.items():
                        if n.lower() in k.lower() or k.lower() in n.lower():
                            g = v
                            break
                if g:
                    out.append(dict(g, name=n, **{"from": "ai"}))
                else:
                    out.append({"name": n, "unit": "g", "amount": 0, "cal": 0, "per": 0,
                                "note": "Didn't recognize that; try another name", "from": "none"})
    # keep the input order
    order = {n: i for i, n in enumerate(clean)}
    out.sort(key=lambda x: order.get(x["name"], 999))
    return {"ok": True, "items": out,
            "local_hits": sum(1 for x in out if x["from"] == "local"),
            "asked": sum(1 for x in out if x["from"] == "ai")}


def load_favs():
    """Saved recipes, per household member -- tastes differ."""
    try:
        with open(FAV_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_favs(d):
    with _lock:
        write_json(FAV_FILE, d)


def _fav_int(v, lo, hi, default):
    try:
        n = int(float(v))
        return n if lo <= n <= hi else default
    except Exception:
        return default


def fav_add(member, item):
    m = safe_member(member)
    store = load_favs()
    lst = store.setdefault(m, [])
    title = str((item or {}).get("title") or "").strip()[:48]
    if not title:
        return {"ok": False, "err": "It needs a name"}
    # same name: update instead of saving twice
    try:
        cal = int(float(item.get("cal") or 0))
    except Exception:
        cal = 0
    rec = {
        "id": "f%d" % int(datetime.datetime.now().timestamp() * 1000),
        "title": title,
        "emoji": str(item.get("emoji") or "🍽️")[:4],
        "desc": str(item.get("desc") or "").strip()[:200],
        "cal": max(0, cal),
        "tags": [str(t)[:16] for t in (item.get("tags") or []) if str(t).strip()][:3],
        "slot": item.get("slot") if item.get("slot") in ("breakfast", "lunch", "dinner") else None,
        "servings": _fav_int((item or {}).get("servings"), 1, 100, 1),
        "total_cal": _fav_int((item or {}).get("total_cal"), 0, 50000, 0),
        "steps": [str(x)[:200] for x in ((item or {}).get("steps") or [])[:30] if str(x).strip()],
        "minutes": _fav_int((item or {}).get("minutes"), 0, 1440, 0),
        "items": [{"name": str(i.get("name") or "")[:32],
                   "amount": str(i.get("amount") or "")[:24],
                   "cal": max(0, int(float(i.get("cal") or 0)))}
                  for i in ((item or {}).get("items") or [])[:8] if isinstance(i, dict) and i.get("name")],
        "source": str((item or {}).get("source") or "")[:200],
        "added": datetime.date.today().isoformat(),
    }
    for i, x in enumerate(lst):
        if x.get("title") == title:
            rec["id"] = x.get("id") or rec["id"]
            rec["added"] = x.get("added") or rec["added"]
            lst[i] = rec
            save_favs(store)
            return {"ok": True, "updated": True, "items": lst}
    lst.insert(0, rec)
    if len(lst) > 200:
        del lst[200:]
    save_favs(store)
    return {"ok": True, "items": lst}


def food_lookup(name, unit="g", amount=100):
    """Calories for one food. My Foods first; otherwise ask the AI and save the answer."""
    name = str(name or "").strip()[:40]
    unit = str(unit or "g").strip()[:8]
    if not name:
        return {"ok": False, "err": "Type a food name"}
    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0
    if amt <= 0:
        amt = 100 if unit in ("g", "ml") else 1

    table = load_lookup()
    # Exact match first, then loose (cooking words, containment). A different unit is not forced:
    # here the user picked the unit, so a same-name entry in another unit counts as not found.
    hit, tunit = lookup_match(table, name, unit, unit_auto=False)
    if hit and tunit == unit:
        return dict({"ok": True, "name": name, "unit": unit, "amount": amt,
                     "per": hit["per"], "cal": int(round(hit["per"] * amt)),
                     "note": hit.get("note", "") + ("" if (hit.get("name") or "").lower() in ("", name.lower()) else " (as \"%s\")" % hit.get("name")),
                     "from": "local"}, **scale_macros(hit, amt))

    if not ai_configured():
        return {"ok": False, "err": "That one isn't in My Foods; looking it up needs AI set up"}
    base = 100 if unit in ("g", "ml") else 1
    prompt = (
        "Estimate calories and macros for this food, as typically prepared at home in the US. Return JSON only, no explanation:\n"
        '{"per": kcal per %s %s, "p": protein grams per %s %s, "c": carbs grams, "f": fat grams, "note": "a few words (under 30 characters, may be empty)"}\n\n'

        "Food: %s"
    ) % (base, "g" if unit == "g" else ("ml" if unit == "ml" else unit),
         base, "g" if unit == "g" else ("ml" if unit == "ml" else unit), name)
    d = _extract_json(_call_llm_text(prompt, fast=True) or "")
    try:
        per_base = float((d or {}).get("per"))
    except Exception:
        return {"ok": False, "err": "Couldn't find it -- you can type a number yourself"}
    if not (0 < per_base <= 9000):
        return {"ok": False, "err": "The number that came back doesn't make sense"}
    per = per_base / base
    entry = {"per": per, "note": str((d or {}).get("note") or "")[:40], "name": name, "unit": unit}
    mac = macro_fields(d)
    if mac:
        entry.update({k: round(v / base, 4) for k, v in mac.items()})
    table[lookup_key(name, unit)] = entry
    save_lookup(table)
    return dict({"ok": True, "name": name, "unit": unit, "amount": amt,
                 "per": per, "cal": int(round(per * amt)),
                 "note": str((d or {}).get("note") or "")[:40], "from": "ai"}, **scale_macros(entry, amt))


def coach_line(stats):
    """The one line under the share card. Gets summary numbers only -- never weight, name or journal text.
    Any failure returns ok:False and the page falls back to its built-in lines; the card works
    without it, so it's not worth holding the card up for.
    """
    if not ai_configured():
        return {"ok": False}
    # Allow-list: only these keys reach the prompt. Weight lost is only sent when the user
    # ticked "show it on the card" -- otherwise the model never sees it.
    safe_keys = ("days", "days_logged", "streak", "workouts", "workout_minutes", "early_nights",
                 "lbs_lost", "last_intention", "this_time", "own_words")
    brief = "\n".join("- %s: %s" % (k, stats.get(k))
                      for k in safe_keys if stats.get(k) not in (None, ""))
    prompt = (
        SAFETY_CLAUSE
        + "You're an old friend who has been around while this person goes about their days -- not a coach, not app copy.\n"
        "They log their routine, meals, workouts and weight on their own computer, and you've been watching along.\n"
        "They made a card to share with friends and want one line from you under it.\n\n"
        "What you know:\n%s\n\n"
        "How to write it:\n"
        "- One sentence, under 18 words. Output just the sentence: no quotes, no explanation\n"
        "- If there's a last_intention, start there -- "
        "you remember what they said they'd do last time, and that's the heart of this line. "
        "If they did it, be glad for them, specifically; if they came close, say it wasn't all done but they kept showing up. No pity, no pep talk\n"
        "- If there are own_words, go along with what they said. Don't rewrite it, judge it, or make it profound\n"
        "- A long streak or real weight lost is worth being glad about -- "
        "but be glad about what they did (showing up daily, how often they moved), not about getting smaller\n"
        "- Few entries: no blame and no cheering; just say they didn't log much, but they were here\n\n"
        "Don't include:\n"
        "- their weight, height or BMI -- you don't have them; don't guess or invent\n"
        "- slogans like \"keep it up\", \"you've got this\", \"crushing it\", or any parallel-structure punchline\n"
        "- comments on body size or looks, pushing them to try harder, or setting their next goal for them\n"
        "- exclamation marks, or \"you're amazing\" talk\n\n"
        "Write it the way a friend would say it in passing. Plain beats peppy.\n" % brief
    )
    raw = _call_llm_text(prompt, max_tokens=300)
    line = (raw or "").strip().strip('"\u201c\u201d').split("\n")[0].strip()
    if not line or len(line.split()) > 24:
        return {"ok": False}
    return {"ok": True, "line": line}


def meal_ideas(slot, question="", context=None, slot_name=""):
    """Ideas for a meal.
    slot is a meal key (breakfast/lunch/dinner or a custom meal), or "any" for ideas
    without a specific meal. slot_name is the display name the page sends, so custom
    meals ("Post-workout") keep their own name.
    """
    if not ai_configured():
        return {"ok": False, "err": "AI isn't set up yet"}
    names = dict(MEAL_NAMES)
    label = str(slot_name or "").strip()[:20] or names.get(slot, "this meal")
    free = (str(slot or "").strip() in ("", "any", "free"))
    ctx = context or {}
    bits = []
    if ctx.get("kcal"):
        bits.append("calorie target for this meal is about %s kcal" % ctx["kcal"])
    if ctx.get("today_intake"):
        bits.append("already eaten today: about %s kcal" % ctx["today_intake"])
    prompt = (
        SAFETY_CLAUSE
        + knowledge_text(ctx.get("member") or "default")
        + ("You're a practical home-cooking advisor. The user wants something to eat; "
           "give three ideas, not tied to a particular meal.\n" if free
           else "You're a practical home-cooking advisor. Give the user three ideas for %s.\n" % label.lower())
        + (("Context: " + "; ".join(bits) + "\n") if bits else "")
        + (("The user adds: %s\n" % str(question)[:300]) if question else "")
        + "\nReturn JSON only, no explanation:\n"
        '{"ideas":[{"emoji":"🥣","title":"name (under 30 characters)","desc":"how to make it, one or two sentences",'

        '"cal":approximate total kcal for the meal (integer),"tags":["10 min","high protein"],'

        '"items":[{"name":"ingredient","amount":"5 oz","cal":kcal for that item (integer)}]}]}\n'

        "Rules:\n"
        "- three ideas, everyday cooking, doable in a normal kitchen\n"
        "- emoji: one fitting food or scene emoji\n"
        "- **items are required**: break the meal into ingredients with how much of each. "
        "The user tracks calories; \"5 oz chicken, 1 cup rice\" is the useful part, a dish name alone isn't\n"
        "- amount in everyday terms: \"5 oz\" or \"150 g\" for meat and veg, \"1 cup\" for rice or pasta, "
        "\"2\" for eggs, \"1 tbsp\" for oil. Never just \"to taste\"\n"
        "- every item has cal; item calories should add up to roughly the meal's cal\n"
        "- three to six items covering the main parts; garlic and spices can be left out\n"
        "- tags: one to three short tags like \"10 min\", \"no cook\", \"high protein\", \"meal prep\"\n"
        "- desc in plain English, no exclamation marks or marketing tone"
    )
    raw = _call_llm_text(prompt, reasoning_ok=True)
    if raw is None:
        # Couldn't reach the model at all -- say so separately from "replied in the wrong format",
        # or people rephrase ten times and get the same message, never thinking to check settings
        return {"ok": False, "err": "Couldn't reach the AI provider. Check the URL and key under AI settings, or try again shortly"}
    d = _extract_json(raw or "", want="ideas")
    arr = (d or {}).get("ideas") if isinstance(d, dict) else None
    raw2 = None
    if not isinstance(arr, list) or not arr:
        # Reasoning models often spend the budget thinking and the answer gets cut off.
        # Rather than hand the failure to the user, retry once: ask for the JSON only,
        # no reasoning, with twice the room.
        raw2 = _call_llm_text(
            prompt + "\n\nImportant: output only the JSON object above -- "
                     "no reasoning, no restating the format, nothing before or after it.",
            max_tokens=8192, reasoning_ok=True)
        d = _extract_json(raw2 or "", want="ideas")
        arr = (d or {}).get("ideas") if isinstance(d, dict) else None
    if not isinstance(arr, list) or not arr:
        llm_trace_dump("meal_ideas slot=%s" % slot, raw, raw2)
        return {"ok": False, "err": "The model didn't reply in the expected format. " + _llm_why()}
    out = []
    for x in arr[:4]:
        if not isinstance(x, dict):
            continue
        try:
            cal = int(float(x.get("cal") or 0))
        except Exception:
            cal = 0
        tags = [str(t)[:16] for t in (x.get("tags") or []) if str(t).strip()][:3]
        items = []
        for it in (x.get("items") or [])[:8]:
            if not isinstance(it, dict):
                continue
            nm = str(it.get("name") or "").strip()[:32]
            if not nm:
                continue
            try:
                ic = int(float(it.get("cal") or 0))
            except Exception:
                ic = 0
            items.append({"name": nm,
                          "amount": str(it.get("amount") or "").strip()[:24],
                          "cal": max(0, ic)})
        # no meal total from the model: use the sum of the items
        if not cal and items:
            cal = sum(i["cal"] for i in items)
        # Cap at 5000: no single meal is that much -- it's a model error or a daily total.
        # Unclamped it pollutes the calorie balance ("998,507 kcal over today").
        if cal > 5000:
            cal = 0
        out.append({"emoji": str(x.get("emoji") or "🍽️")[:4],
                    "title": str(x.get("title") or "").strip()[:40],
                    "desc": str(x.get("desc") or "").strip()[:200],
                    "cal": max(0, cal), "tags": tags, "items": items})
    if not out:
        return {"ok": False, "err": "Couldn't come up with anything -- try rephrasing"}
    return {"ok": True, "ideas": out}


def food_advice(question, image_b64=None, context=None):
    """Meal advice. A question alone, or with a photo of ingredients / the fridge.
    Advice only, nothing is written -- if the user likes it, they log it or plan it themselves.
    """
    if not ai_configured():
        return {"ok": False, "err": "AI isn't set up yet"}
    ctx = context or {}
    bits = []
    if ctx.get("target_kcal"):
        bits.append("daily calorie target about %s kcal" % ctx["target_kcal"])
    if ctx.get("dinner_kcal"):
        bits.append("dinner target about %s kcal" % ctx["dinner_kcal"])
    if ctx.get("today_intake"):
        bits.append("already eaten today about %s kcal" % ctx["today_intake"])
    ctx_txt = ("About the user: " + "; ".join(bits) + ".\n") if bits else ""
    prompt = (
        SAFETY_CLAUSE +
        "You're a practical home-cooking advisor. The user is asking what to eat; give specific, doable suggestions.\n\n"
        + knowledge_text(ctx.get("member") or "default").replace("%", "%%")
        + ctx_txt.replace("%", "%%") +
        "The question: %s\n\n"
        "Requirements:\n"
        "- get straight to the suggestions; don't restate the question\n"
        "- use what they already have before sending them shopping\n"
        "- everyday cooking, a normal kitchen, no special equipment or hard-to-find ingredients\n"
        "- give rough portions and calorie estimates, marked as estimates\n"
        "- at most three options, two or three sentences each on how to make it\n"
        "- plain English, no exclamation marks or marketing tone"
    ) % str(question)[:800]
    if image_b64:
        cfg = load_llm_config()
        model = cfg.get("vision_model") or cfg.get("model") or ""
        b64 = str(image_b64)
        if "," in b64[:64]:
            b64 = b64.split(",", 1)[1]
        content = _llm_chat([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": _image_url(b64)}},
            {"type": "text", "text": prompt},
        ]}], model, timeout=90)
    else:
        content = _call_llm_text(prompt)
    if not content:
        return {"ok": False, "err": "No reply -- try again in a moment"}
    return {"ok": True, "advice": content.strip()[:3000]}


INTENT_PROMPT = (
    "Decide which kind of message this is. Return JSON only, no explanation:\n"
    '{"intent": "update" | "record" | "meal" | "chat"}\n\n'
    "update: wants to change the app's setup -- daily rhythm, habits, menu, rules, goals and so on. "
    "Sounds like \"change X to Y\", \"add a...\", \"remove...\".\n"
    "record: wants to log facts about today -- a workout, sleep, weight, a finished habit, "
    "or any of these together with meals.\n"
    "meal: only says what they ate, to log it. Sounds like \"breakfast was...\", \"for lunch I had...\".\n"
    "chat: everything else -- questions, asking for advice, small talk, feelings.\n\n"
    "%sUser: %s"
)


# Low moments. Found while testing: "Got yelled at today, ate a whole bag of chips... I'm so useless,
# forget it, I quit" was matched as a meal log because of "ate", and the user got a form asking them to
# confirm the calories in the chips. That's the moment someone most needs to be heard, and a form is the
# worst possible answer. These always go to conversation first. Two kinds, answered differently:
#  lapse -- overate, wants to quit, beating themselves up: acknowledge, one day changes little, don't starve to make up;
#  worry -- the scale is stressing them (up again, stuck for weeks): acknowledge, then explain with their own data.
# Answering "why is it up again?" with lapse words ("one big day isn't failure") misses the point.
_LAPSE_RE = re.compile(
    r"\b(useless|worthless|pathetic|a failure|failed again|give up|giving up|i quit|what'?s the point|"
    r"binged?|binging|blew it|ruined|messed up|couldn'?t stop|can'?t stop|out of control|overate|stuffed myself|"
    r"hate myself|disgusting|ashamed|so guilty|guilt|crying|cried|hopeless)\b", re.I)
_WORRY_RE = re.compile(
    r"(\bwhy (am i|is it|did i).{0,20}(heavier|gain|up)|\bup again\b|\bgained\b|\bheavier\b|\bnot losing\b|"
    r"won'?t (budge|move)|isn'?t moving|\bstuck\b|\bplateau|\bstalled\b|frustrat|anxious|stressed|discouraged)", re.I)


# craving -- hasn't eaten yet, wants to ("kids are asleep and I really want chips"). The useful answer is one or
# two things to do right now, plus how long they've been fasting / when the window opens. Lapse comfort is premature.
_CRAVE_RE = re.compile(
    r"(\bcraving|\bcrave\b|so hungry|starving|dying for|\bsnacky\b|munchies|"
    r"really want (some )?(chips|cookies|ice cream|candy|chocolate|pizza|a snack|something sweet))", re.I)
_ATE_RE = re.compile(r"\b(ate|eaten|finished|devoured|drank|had (a|an|some|the|two|three|\d))\b", re.I)


def distress_kind(text):
    t = str(text or "")
    if _CRAVE_RE.search(t) and not _ATE_RE.search(t):
        return "craving"
    # "stuck for weeks, is this pointless, am I failing again" is about the scale, not about overeating
    if _WORRY_RE.search(t) and not _ATE_RE.search(t) and not re.search(r"\b(binge|binged|stuffed|couldn'?t stop|blew it)\b", t, re.I):
        return "worry"
    if _LAPSE_RE.search(t):
        return "lapse"
    if _WORRY_RE.search(t):
        return "worry"
    return None


def is_distress(text):
    return bool(distress_kind(text))


def _rule_intent(text):
    """High-confidence intents by rule first, so a log entry is never mistaken for a settings change."""
    t = str(text or "").strip()
    if not t:
        return None
    if is_distress(t):
        return "chat"
    update_verb = re.search(r"\b(change|set|update|adjust|add|remove|delete|rename|replace|move|swap)\b", t, re.I)
    config_word = re.search(r"\b(rhythm|schedule|routine|menu|plate|template|habits?|checklist|rules?|reminders?|"
                            r"daily tip|goals?|targets?|exercise types?|categor(y|ies)|nickname)\b", t, re.I)
    if update_verb and config_word:
        return "update"
    return record_kind(t)


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"


def record_kind(text):
    """Does this contain something to log? workout/sleep/weight/habit -> record; only food/drink -> meal."""
    t = str(text or "").strip()
    has_meal = bool(re.search(
        r"\b(breakfast|lunch|dinner|supper|snack|brunch)\b\s*(:|was|were|i had|i ate|had|-)|"
        r"\bfor (breakfast|lunch|dinner|supper|a snack)\b|\bi (ate|had|drank)\b", t, re.I))
    has_other_record = bool(re.search(
        r"\b(walk(ed)?|walking|ran|run|jog(ged)?|swim|swam|bike|biked|cycled|cycling|spin|yoga|pilates|"
        r"lift(ed)?|weights|gym|workout|worked out|hiked?|elliptical|rowing|stretch(ed)?)\b"
        r"[^.;]{0,30}?\d+(\.\d+)?\s*(min|mins|minutes|hours?|hrs?|steps|k steps|miles?|mi|km)\b|"
        r"\b\d{1,3}(,\d{3}|k)?\s*steps\b|"
        r"\b(slept|went to bed|woke up|fell asleep|bedtime|in bed)\b.{0,30}(\d{1,2}(:\d{2})?\s*(am|pm)?|hours?)|"
        r"\b(weigh(ed)?|weight|scale|weigh-in)\b[^.;]{0,14}\d{2,3}(\.\d+)?|"
        r"\b\d{2,3}(\.\d+)?\s*(lb|lbs|pounds|kg)\b|"
        r"\b" + _MONTH + r"\s*\d{1,2}[^.\n]{0,16}?\d{2,3}(\.\d+)?|"     # a multi-day weight log: "Aug 25: 176.4"
        r"\b\d{1,2}/\d{1,2}\b[^\n]{0,12}?\b\d{2,3}(\.\d+)?\b|"
        r"\b(done|did|finished|completed|checked off)\b.{0,24}\b(water|workout|walk|stretch|habit|steps)|"
        r"\b(log|record|save) (it|this|that|them)\b", t, re.I))
    if has_other_record:
        return "record"
    if has_meal:
        return "meal"
    return None


def _looks_like_daily_record(text):
    # Emotional messages are routed to chat by _rule_intent; this only asks "is today being logged?"
    return _rule_intent(text) in ("record", "meal") or (is_distress(text) and bool(record_kind(text)))


def classify_intent(text, context=""):
    """What does the user want? When unsure, it's chat.
    Misreading as chat costs one missed shortcut; misreading as update changes their settings.
    The costs aren't equal, so uncertainty goes to conversation.
    """
    t = str(text or "")
    if not t.strip():
        return "chat"
    ruled = _rule_intent(t)
    if ruled:
        return ruled
    # "log that" needs the previous message; only a short slice, to keep prompts bounded.
    ctx = str(context or "").strip()[:400]
    if ctx and re.search(r"\b(log|record|save|add) (it|this|that|them)\b", t, re.I):
        ruled = _rule_intent(ctx + "; " + t)
        if ruled:
            return ruled
    if not ai_configured():
        return "chat"
    ctx_line = ("Previous message: %s\n" % ctx) if ctx else ""
    d = _extract_json(_call_llm_text(INTENT_PROMPT % (ctx_line, t[:400]), fast=True) or "")
    got = (d or {}).get("intent") if isinstance(d, dict) else None
    return got if got in ("update", "record", "meal", "chat") else "chat"


def kcal_for_foods(foods):
    """Calories for "food + amount" items.
    My Foods first; only what's missing goes to the AI -- "100 g chicken breast" looked up once
    should be remembered, not asked again every time. When everything is in My Foods, nothing goes
    online and no API key is needed.
    """
    table = load_lookup()
    hits, misses = {}, []
    items = []
    for f in (foods or []):
        if not isinstance(f, dict):
            continue
        nm = str(f.get("name") or "").strip()[:40]
        try:
            wt = int(float(f.get("weight") or 0))
        except Exception:
            wt = 0
        u = str(f.get("unit") or "g").strip()[:8]
        if nm:
            items.append({"name": nm, "weight": max(0, wt), "unit": u,
                          "unit_auto": bool(f.get("unit_auto"))})
    if not items:
        return {"ok": False, "err": "Add a food name first"}

    # My Foods first: hits are converted per unit; only misses go to the AI.
    # Matching is loose: cooking words, same name in another unit, containment (see lookup_match).
    for idx, x in enumerate(items):
        rec, tunit = lookup_match(table, x["name"], x["unit"], x.get("unit_auto"))
        if rec:
            amt = x["weight"]
            if tunit != x["unit"]:
                # Took the list's unit. No amount -> one serving / 100 g; an amount the user typed is kept
                # (someone who typed "1" without changing the unit almost certainly means one egg)
                if not amt or (tunit in PIECE_UNITS and amt > 20):
                    amt = 100 if tunit in ("g", "ml") else 1
            amt = amt or (100 if tunit in ("g", "ml") else 1)
            hits[idx] = dict({"name": x["name"], "weight": amt, "unit": tunit,
                              "cal": int(round(rec["per"] * amt)), "from": "local",
                              "matched": rec.get("name") if rec.get("name") != x["name"] else None},
                             **scale_macros(rec, amt))
        else:
            misses.append(idx)

    if not misses:
        # all found in My Foods; nothing goes online
        return {"ok": True, "from": "local",
                "foods": [hits[i] for i in range(len(items))]}

    if not ai_configured():
        # Some are missing and there's no key: return what we found and explain the rest
        if hits:
            out = []
            for i, x in enumerate(items):
                out.append(hits[i] if i in hits
                           else {"name": x["name"], "weight": x["weight"], "cal": 0})
            return {"ok": True, "from": "partial", "foods": out,
                    "note": "%d item(s) aren't in My Foods; calculating them needs AI" % len(misses)}
        return {"ok": False, "err": "None of these are in My Foods; looking them up needs AI set up"}

    ask = [items[i] for i in misses]

    def _amt(x):
        if not x["weight"]:
            return "(no amount given -- assume a typical serving)"
        u = x.get("unit") or "g"
        return ("%d g" % x["weight"]) if u == "g" else ("%d %s" % (x["weight"], u))
    lines = "\n".join("- %s %s" % (x["name"], _amt(x)) for x in ask)
    prompt = (
        "Estimate the calories for these foods, as typically prepared at home in the US.\n\n%s\n\n"
        "Return JSON only, no explanation, in the same order as above:\n"
        '{"foods":[{"name":"food name","weight":amount,"cal":kcal,"p":protein_g,"c":carbs_g,"f":fat_g}]}\n'

        "Rules: no amount -> assume a typical serving; units like each/slice/cup -> typical sizes;\n"
        "calories as integers; p, c, f are grams of protein, carbohydrate and fat in that amount, one decimal;\n"
        "return names unchanged; return weight exactly as the user gave it -- don't convert to grams."
    ) % lines
    content = _call_llm_text(prompt, fast=True)
    d = _extract_json(content or "")
    arr = (d or {}).get("foods") if isinstance(d, dict) else None
    if not isinstance(arr, list) or not arr:
        return {"ok": False, "err": "Couldn't calculate it -- you can type the number yourself", "raw": (content or "")[:300]}
    # Map AI answers back onto the misses in order, and save them to My Foods for next time
    got = {}
    for i, x in enumerate(arr):
        if not isinstance(x, dict) or i >= len(misses):
            continue
        idx = misses[i]
        base = items[idx]
        try:
            cal = int(float(x.get("cal") or 0))
        except Exception:
            cal = 0
        try:
            wt = int(float(x.get("weight") or 0))
        except Exception:
            wt = 0
        amt = max(0, wt or base.get("weight") or 0) or (100 if base["unit"] in ("g", "ml") else 1)
        mac = macro_fields(x)
        got[idx] = dict({"name": str(x.get("name") or base.get("name") or "").strip()[:40],
                         "weight": amt, "cal": max(0, cal), "from": "ai"}, **mac)
        if cal > 0 and amt > 0:
            entry = {"per": cal / amt, "name": base["name"], "unit": base["unit"], "note": ""}
            if mac:
                entry.update({k: round(v / amt, 4) for k, v in mac.items()})   # per unit; scaled back up next time
            table[lookup_key(base["name"], base["unit"])] = entry
    if got:
        save_lookup(table)

    out = []
    for i in range(len(items)):
        if i in hits:
            out.append(hits[i])
        elif i in got:
            out.append(got[i])
        else:
            out.append({"name": items[i]["name"], "weight": items[i]["weight"], "cal": 0})
    return {"ok": True, "foods": out,
            "from": "mixed" if hits else "ai",
            "local_hits": len(hits), "asked": len(got)}


def exercise_kcal(ex_type, duration, intensity, weight_kg):
    """Calories burned from exercise type, duration, intensity and *current* weight.
    A fixed rate per minute ignores body weight -- the same 40-minute walk burns quite
    differently at 110 lb and at 200 lb.
    """
    key = ai_configured()
    if not key:
        return {"ok": False, "err": "AI isn't set up yet"}
    try:
        dur = int(float(duration or 0))
    except Exception:
        dur = 0
    if dur <= 0:
        return {"ok": False, "err": "Add the duration first"}
    w = None
    try:
        w = float(weight_kg)
    except Exception:
        pass
    prompt = (
        "Estimate the calories burned in this workout. Return JSON only, no explanation:\n"
        '{"kcal": integer}\n\n'

        "Activity: %s\nDuration: %d minutes\nIntensity: %s\n%s"
        "Use standard MET values and give one reasonable integer."
    ) % (str(ex_type or "exercise")[:30], dur, str(intensity or "moderate")[:12],
         ("Body weight: %.1f kg\n" % w) if w and 20 <= w <= 300 else "")
    content = _call_llm_text(prompt, fast=True)
    d = _extract_json(content or "")
    try:
        kcal = int(float((d or {}).get("kcal")))
    except Exception:
        return {"ok": False, "err": "Couldn't calculate it -- you can type a number", "raw": (content or "")[:200]}
    if not (0 < kcal <= 5000):
        return {"ok": False, "err": "That number doesn't look right; please enter it yourself"}
    return {"ok": True, "kcal": kcal}


def meals_from_text(text, meal_keys):
    """Split a free-text description of meals into foods and calories per meal.
    Same parsing and saving as photo recognition, with text instead of an image.
    Returns {meal_key: {"foods":[...], "cal":int, "items":"..."}}, or {} if nothing parsed.
    This only calculates and shows; the user decides on the confirm screen whether it's saved --
    model estimates can be well off and shouldn't land in the data on their own.
    """
    key = ai_configured()
    if not key:
        return {"ok": False, "err": "AI isn't set up yet"}
    allowed = ", ".join(meal_keys)
    prompt = (
        SAFETY_CLAUSE +
        "The user will describe what they ate today. Work out which meal each food belongs to "
        "and estimate the amount (grams) and calories (kcal).\n\n"
        "It is now %s.\n"
        "The user says: %s\n\n"
        "Return JSON only, no explanation:\n"
        '{"meals": {"lunch": {"at":"12:00", "foods": [{"name":"food","weight":grams,"cal":kcal,"p":protein_g,"c":carbs_g,"f":fat_g}]}}}\n'

        "Meal keys can only be: %s.\n"
        "Rules:\n"
        "- if the user says when they ate, return at for that meal (24-hour HH:MM); otherwise leave at out\n"
        "- water, black coffee and unsweetened tea without milk aren't a meal\n"
        "- if they say \"last night\" or \"yesterday\", or the time they give is later than now, return \"day\": -1 for that meal\n"
        "- if they don't say which meal, put it where it most likely belongs\n"
        "- no amount given -> assume a typical serving\n"
        "- estimate calories for typical American home cooking or the named restaurant\n"
        "- don't include meals they didn't mention\n"
        "- short names (\"turkey sandwich\", not \"a turkey sandwich on wheat bread that I made\")"
    ) % (_now_text(), str(text)[:1500], allowed)
    content = _call_llm_text(prompt, fast=True)
    d = _extract_json(content or "")
    if not isinstance(d, dict):
        return {"ok": False, "err": "Couldn't make sense of that", "raw": (content or "")[:300]}
    raw = d.get("meals") if isinstance(d.get("meals"), dict) else d
    out = {}
    for k, v in (raw or {}).items():
        if k not in meal_keys or not isinstance(v, dict):
            continue
        foods = []
        for f in (v.get("foods") or []):
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "").strip()[:40]
            try:
                cal = int(float(f.get("cal") or 0))
            except Exception:
                cal = 0
            try:
                wt = int(float(f.get("weight") or 0))
            except Exception:
                wt = 0
            if name or cal > 0:
                foods.append(dict({"name": name, "weight": max(0, wt), "cal": max(0, cal)}, **macro_fields(f)))
        if foods:
            out[k] = {"foods": foods,
                      "cal": sum(x["cal"] for x in foods),
                      "items": ", ".join(x["name"] for x in foods if x["name"])}
            if clean_hm(v.get("at")):
                out[k]["at"] = clean_hm(v.get("at"))
            try:
                if int(float(v.get("day") or 0)) == -1:
                    out[k]["day"] = -1
            except Exception:
                pass
    if not out:
        return {"ok": False, "err": "Couldn't tell which meal had what -- try saying it another way",
                "raw": (content or "")[:300]}
    return {"ok": True, "meals": out}


def _maybe_from_lb(kg, text):
    """The prompt asks for kilograms, but a model sometimes copies "182 lb" through as 182.
    If the user's text talks in pounds and contains that exact number, convert it."""
    if kg is None or text is None:
        return kg
    t = str(text)
    if not re.search(r"\b(lb|lbs|pounds?)\b", t, re.I) or re.search(r"\b(kg|kilos?|kilograms?)\b", t, re.I):
        return kg
    for m in re.finditer(r"\d{2,3}(?:\.\d+)?", t):
        try:
            if abs(float(m.group(0)) - kg) < 0.051:
                return round(kg / 2.20462, 2)
        except Exception:
            pass
    return kg


def _sanitize_daily_log(raw, meal_keys, habit_count=0, exercise_types=None, meal_names=None, text=None):
    """Clamp model output into shapes the page can safely accept.
    meal_names: {display name: key}. If the model writes "Lunch" as the key, or copies a placeholder
    from the template, the meal is recovered by name. text: the user's own words -- a model once put a
    food's grams into the top-level weight, so a body weight is ignored unless the text mentions weight."""
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("log"), dict):
        raw = raw["log"]
    result = {}
    meal_names = meal_names or {}

    meals = {}
    meal_raw = raw.get("meals") if isinstance(raw.get("meals"), dict) else {}
    # Sometimes meals comes back as a list [{"meal":"lunch",...}]; accept that too
    if not meal_raw and isinstance(raw.get("meals"), list):
        meal_raw = {}
        for item in raw["meals"]:
            if isinstance(item, dict):
                k = item.get("key") or item.get("meal") or item.get("name") or ""
                meal_raw[str(k)] = item
    for key, value in meal_raw.items():
        if not isinstance(value, dict):
            continue
        if key not in meal_keys:
            # "Lunch" -> lunch; a copied placeholder -> look for a name/meal field in the value
            nm = str(value.get("name") or value.get("meal") or key or "").strip()
            key = meal_names.get(nm) or meal_names.get(nm.lower()) or meal_names.get(key) or (nm.lower() if nm.lower() in meal_keys else None)
            if key not in meal_keys or key in meals:
                continue
        foods = []
        for food in (value.get("foods") or []):
            if not isinstance(food, dict):
                continue
            name = str(food.get("name") or "").strip()[:40]
            try:
                weight = max(0, min(5000, int(float(food.get("weight") or 0))))
            except Exception:
                weight = 0
            try:
                kcal = max(0, min(5000, int(float(food.get("cal") or 0))))
            except Exception:
                kcal = 0
            if name or kcal:
                foods.append(dict({"name": name, "weight": weight, "cal": kcal}, **macro_fields(food)))
        if foods:
            meals[key] = {"foods": foods,
                          "cal": sum(x["cal"] for x in foods),
                          "items": ", ".join(x["name"] for x in foods if x["name"])}
            at = clean_hm(value.get("at"))
            if at:
                meals[key]["at"] = at
            try:
                if int(float(value.get("day") or 0)) == -1:
                    meals[key]["day"] = -1        # eaten yesterday; editable on the confirm screen
            except Exception:
                pass
    if meals:
        result["meals"] = meals

    allowed_types = [str(x).strip()[:30] for x in (exercise_types or []) if str(x).strip()]
    exercises = []
    for item in (raw.get("exercises") or []):
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "").strip()[:30]
        if allowed_types and typ not in allowed_types:
            alias = {"walking": "Walk", "walked": "Walk", "hike": "Hike", "hiking": "Hike", "running": "Run",
                     "jog": "Run", "jogging": "Run", "cycling": "Bike", "biking": "Bike", "spin": "Bike",
                     "swimming": "Swim", "weights": "Strength", "lifting": "Strength", "gym": "Strength",
                     "weight training": "Strength", "pilates": "Yoga"}
            typ = next((t for t in allowed_types if t.lower() == typ.lower()), alias.get(typ.lower(), typ))
            if typ not in allowed_types:
                typ = "Other" if "Other" in allowed_types else allowed_types[0]
        try:
            duration = max(0, min(1440, int(float(item.get("duration") or 0))))
        except Exception:
            duration = 0
        try:
            steps = max(0, min(200000, int(float(item.get("steps") or 0))))
        except Exception:
            steps = 0
        try:
            raw_kcal = int(float(item.get("kcal") or 0))
            kcal = raw_kcal if 0 <= raw_kcal <= 5000 else 0
        except Exception:
            kcal = 0
        intensity = str(item.get("intensity") or "moderate").strip().lower()
        if intensity not in ("light", "moderate", "vigorous"):
            intensity = "moderate"
        if typ and (duration or steps):
            exercises.append({"type": typ, "duration": duration, "steps": steps,
                              "intensity": intensity, "kcal": kcal})
    if exercises:
        result["exercises"] = exercises

    sleep = raw.get("sleep")
    if isinstance(sleep, dict):
        bed = str(sleep.get("bed") or "").strip()
        wake = str(sleep.get("wake") or "").strip()
        valid_time = lambda x: bool(re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", x))
        try:
            quality = max(1, min(5, int(float(sleep.get("quality") or 3))))
        except Exception:
            quality = 3
        if valid_time(bed) and valid_time(wake):
            result["sleep"] = {"bed": bed, "wake": wake, "quality": quality}

    # Daytime naps are kept separately. Putting one in sleep would overwrite the night's sleep
    # and the day's sleep total would become twenty minutes.
    naps = []
    for item in (raw.get("naps") or []):
        if not isinstance(item, dict):
            continue
        start = str(item.get("start") or "").strip()
        end = str(item.get("end") or "").strip()
        s_min, e_min = _hm_to_min(start), _hm_to_min(end)
        if s_min is None:
            start = ""
        if e_min is None:
            end = ""
        try:
            minutes = int(float(item.get("minutes") or 0))
        except Exception:
            minutes = 0
        if not minutes and s_min is not None and e_min is not None:
            minutes = e_min - s_min
            if minutes < 0:
                minutes += 1440
        minutes = max(0, min(600, minutes))
        if minutes:
            naps.append({"start": start, "end": end, "minutes": minutes})
    if naps:
        result["naps"] = naps[:4]

    # Body weight: the prompt calls it body_kg; plain "weight" is accepted only if the text mentions weight
    wv = raw.get("body_kg")
    if wv in (None, ""):
        wv = raw.get("weight")
        if text is not None and not re.search(r"\b(weigh|weight|weighed|scale|kg|kilos?|lb|lbs|pounds?)\b", str(text), re.I):
            wv = None
    try:
        weight = round(float(wv), 2)
    except Exception:
        weight = None
    # The model copied pounds through without converting: convert if the text is in pounds
    weight = _maybe_from_lb(weight, text)
    if weight is not None and 30 <= weight <= 250:
        result["weight"] = weight

    # Catching up on past weights: several days at once. Valid dates, not in the future; last one per day wins.
    today_s = datetime.date.today().isoformat()
    wlist = []
    seen_d = set()
    for item in (raw.get("weights") or [])[:120]:
        if not isinstance(item, dict):
            continue
        d_ = str(item.get("date") or "").strip()[:10]
        if not _DATE_RE.match(d_) or d_ > today_s:
            continue
        try:
            kg = round(float(item.get("kg") if item.get("kg") is not None else item.get("weight")), 2)   # some scales give two decimals
        except Exception:
            continue
        kg = _maybe_from_lb(kg, text)
        if not (30 <= kg <= 250):
            continue
        if d_ in seen_d:
            wlist = [x for x in wlist if x["date"] != d_]
        seen_d.add(d_)
        wlist.append({"date": d_, "kg": kg, "approx": bool(item.get("approx"))})
    if wlist:
        wlist.sort(key=lambda x: x["date"])
        result["weights"] = wlist
        # today's entry doubles as the day's weight
        if wlist[-1]["date"] == today_s and "weight" not in result:
            result["weight"] = wlist[-1]["kg"]

    habits = []
    for item in (raw.get("habits") or []):
        idx = item.get("index") if isinstance(item, dict) else item
        try:
            idx = int(idx)
        except Exception:
            continue
        if 0 <= idx < habit_count and idx not in habits:
            habits.append(idx)
    if habits:
        result["habits"] = habits
    return result


def daily_log_from_text(text, meal_specs=None, habit_specs=None, exercise_types=None):
    """Split one message into today's log entries of several kinds. Parses and previews only; nothing is saved."""
    if not ai_configured():
        return {"ok": False, "err": "AI isn't set up yet"}
    meal_specs = meal_specs or [{"key": k, "name": k} for k in allowed_meals()]
    meals = []
    for item in meal_specs:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            name = str(item.get("name") or key).strip()[:20]
        else:
            key = str(item).strip()
            name = key
        if key in allowed_meals() and key not in [x[0] for x in meals]:
            meals.append((key, name))
    if not meals:
        meals = [(k, k) for k in allowed_meals()]
    habits = [str(x).strip()[:80] for x in (habit_specs or []) if str(x).strip()][:80]
    ex_types = [str(x).strip()[:30] for x in (exercise_types or []) if str(x).strip()][:50]
    if not ex_types:
        ex_types = ["Walk", "Run", "Bike", "Swim", "Strength", "Yoga", "Hike", "Other"]
    meal_desc = ", ".join("%s=%s" % x for x in meals)
    habit_desc = "\n".join("%d. %s" % (i, x) for i, x in enumerate(habits)) or "(no habits to match)"
    prompt = (
        "You turn what a user says about their day into structured log entries for a health journal app. "
        "Extract only facts the user actually stated; never add items they didn't mention.\n\n"
        "It is now %s.\n"
        "The user says: %s\n\n"
        "Meal keys: %s\n"
        "Exercise types: %s\n"
        "Their habits (return the number in front):\n%s\n\n"
        "Return JSON only. No explanation, and don't work through each food in your reply -- rough amounts and calories are fine:\n"
        '{"meals":{"%s":{"at":"12:00","day":0,"foods":[{"name":"food","weight":grams,"cal":kcal,"p":protein_g,"c":carbs_g,"f":fat_g}]}},'
        '"exercises":[{"type":"Walk","duration":30,"steps":3000,"intensity":"moderate","kcal":0}],'
        '"sleep":{"bed":"23:30","wake":"07:00","quality":3},'
        '"naps":[{"start":"13:00","end":"13:20","minutes":20}],'
        '"body_kg":75.6,"weights":[{"date":"2026-08-25","kg":80.0,"approx":false}],"habits":[0,3]}\n'
        "Rules:\n"
        "- Leave out any field that wasn't mentioned; one message can fill several. meals keys must be from the meal keys above.\n"
        "- A food's weight is grams of that food. The user's own body weight goes in body_kg (kilograms); no body weight mentioned, no body_kg.\n"
        "- For meals, estimate grams, calories, and protein p, carbs c, fat f (grams, one decimal) per food; no amount given -> a typical serving.\n"
        "- Body weight is always in kg: \"180 lb\" or \"180 pounds\" is 81.6 (divide by 2.2046); \"78 kg\" is 78. "
        "\"Lost 3 pounds\" is not a body weight -- no body_kg.\n"
        "- Only today's weight -> body_kg. Several days of weights (catching up, often a pasted table) -> one entry each in weights, "
        "date as YYYY-MM-DD (infer the year from \"now\"; a month that hasn't come yet this year means last year); "
        "\"early September\" -> the 3rd, \"mid\" -> the 15th, \"end of\" -> the 28th; a range (171.2–172.6) -> the midpoint; "
        "set approx true for those two cases; include today's entry in weights too.\n"
        "- If they say when they ate (\"at noon\", \"7:30pm\", \"around 8\"), return at for that meal (24-hour HH:MM; "
        "\"around 8\" -> 08:15 or 20:15 by context). No time mentioned -> no at; don't guess.\n"
        "- Water, black coffee, and plain tea without milk or sugar have almost no calories and are not a meal; leave them out of meals.\n"
        "- day is which day the meal was: today is 0. \"Last night\", \"yesterday\", or a time later than now "
        "(it's 8am and they say \"dinner at 7\") means yesterday: -1. Shift workers often mention last night's food with this morning's; keep them apart.\n"
        "- Each meal key appears once. Two extra eating occasions (say 12:30am and 4am): if there's a meal named like "
        "\"Night meal\" or \"Late snack\", put the one between 9pm and 5am there and the other in snack; if both are at night, the earlier one "
        "goes to the night meal. Without such a meal, merge them into snack with the earlier time as at.\n"
        "- duration always in minutes; steps only if they said a step count.\n"
        "- kcal only if they stated calories burned; otherwise 0 (it's estimated locally).\n"
        "- Sleep times in 24-hour HH:MM; quality: great=5, good=4, okay=3, poor=2, terrible=1. "
        "If either bedtime or wake time is missing, leave sleep out.\n"
        "- sleep is only the night's sleep. Daytime naps, power naps and dozing off go in naps, never in sleep. "
        "Only a length given -> fill minutes only, leave start and end empty; a time range -> fill start and end.\n"
        "- habits: only existing habits they explicitly said they did. Logging a workout or a meal does not "
        "tick a habit by itself.\n"
        "- If they're asking to change goals, the routine or templates, return an empty object."
    ) % (_now_text(), str(text)[:2000], meal_desc, ", ".join(ex_types), habit_desc, meals[0][0])
    # Long messages (six foods for lunch plus dinner) are where models trip: reasoning models put the answer
    # in reasoning_content, restate the template first, or run out of tokens mid-JSON. We want JSON and clean
    # it afterwards, so reasoning text is searched too; if nothing parses, ask once more; and the error says
    # what actually happened instead of "couldn't find anything to log", which blames the user.
    # Generous token budget: reasoning models share max_tokens between thinking and the answer, and can
    # spend 2,000 words deliberating over a bagel. Longer timeout for the same reason.
    content = _call_llm_text(prompt, max_tokens=12000, reasoning_ok=True, timeout=150, fast=True)
    data = _extract_json(content or "", want=_DAILY_LOG_KEYS)
    content2 = None
    fin1, _e1 = _last_llm_finish()
    if data is None:
        content2 = _call_llm_text(prompt + "\n\nOnce more: don't deliberate. Return one JSON object only -- no explanation, no restating the template. Rough amounts and calories are fine.",
                                  max_tokens=24000, reasoning_ok=True, timeout=180, fast=True)
        data = _extract_json(content2 or "", want=_DAILY_LOG_KEYS)
        llm_trace_dump("daily_log", content, content2)
    fin2, err2 = _last_llm_finish()
    name_map = {}
    for k, nm in meals:
        name_map[nm] = k
        name_map[nm.lower()] = k
        name_map[k] = k
    clean = _sanitize_daily_log(data, [x[0] for x in meals], len(habits), ex_types,
                                meal_names=name_map, text=text)
    if not clean:
        if data is None:
            if "length" in (fin1, fin2):
                err = ("The model spent its whole budget reasoning and never wrote the answer, even with 20,000 tokens. "
                       "Under AI settings, set a small non-reasoning model for logging (gpt-5-nano, claude-haiku-5-5, gemini-2.5-flash-lite) -- "
                       "or split this into two messages.")
            elif not ((content or "").strip() or (content2 or "").strip()):
                err = "The model returned no text. " + _llm_why()
            elif "timed out" in err2 or "timeout" in err2.lower():
                err = "The model was still thinking after three minutes. Try a faster model for logging under AI settings, or split this into two messages."
            else:
                err = ("The model replied twice, and neither reply was valid JSON. Try again, or split this into two messages; "
                       "the raw replies are saved in .llm_debug.txt in the app folder.")
        else:
            llm_trace_dump("daily_log empty after cleaning", content, content2)
            err = "Nothing came out of that -- the model didn't find any meals, workouts, sleep, weight or habits in it. Try again, e.g. \"Lunch: ...; dinner at 7: ...\"."
        return {"ok": False, "err": err, "raw": (content2 or content or "")[:300]}
    return {"ok": True, "log": clean}


# Top-level keys a log result can have: _extract_json picks the last object containing one of these
_DAILY_LOG_KEYS = ("meals", "exercises", "sleep", "naps", "body_kg", "weight", "weights", "habits")


def _call_llm_text(prompt, max_tokens=4096, reasoning_ok=False, timeout=90, fast=False):
    """Call the text model with a single user message. Returns the text or None.
    reasoning_ok is only for callers that want JSON and run it through _extract_json.
    Anything shown to the user directly keeps it False.
    fast=True uses the logging model (structured output: meals, intent, calories), see fast_model_of().
    """
    cfg = load_llm_config()
    model = fast_model_of(cfg) if fast else (cfg.get("model") or "")
    return _llm_chat([{"role": "user", "content": prompt}], model, 0.3,
                     timeout=timeout, max_tokens=max_tokens, reasoning_ok=reasoning_ok)


def _last_llm_finish():
    with _lock:
        last = _LLM_TRACE[-1] if _LLM_TRACE else None
    return (str(last.get("finish") or ""), str(last.get("err") or "")) if last else ("", "")


def _call_llm_messages(messages):
    """Call the text model with a multi-turn conversation (system/user/assistant). Returns the text or None."""
    cfg = load_llm_config()
    return _llm_chat(messages, cfg.get("model") or "", 0.7, max_tokens=2048)


def _bmr_value(weight, prof):
    """Basal metabolic rate. Same formula as calcBMR on the page -- change one, change both.
    Mifflin-St Jeor with sex, age and height; any missing -> a per-kilogram factor.
    """
    try:
        w = float(weight or 0)
    except Exception:
        w = 0.0
    if w <= 0:
        return 0
    prof = prof or {}
    try:
        h = float(prof.get("height_cm") or 0)
        a = float(prof.get("age") or 0)
    except Exception:
        h, a = 0.0, 0.0
    sx = prof.get("sex")
    if sx in ("male", "female") and 100 <= h <= 250 and 10 <= a <= 100:
        base = 10 * w + 6.25 * h - 5 * a + (5 if sx == "male" else -161)
        return max(800, int(round(base)))
    try:
        k = float(prof.get("bmr_per_kg") or 22)
    except Exception:
        k = 22.0
    return int(round(w * k))


ACTIVITY_FACTOR = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55,
                   "active": 1.725, "very_active": 1.9}


def _daily_burn(weight, prof, exercise_kcal):
    """Total burn for the day = BMR x activity factor + exercise."""
    bmr = _bmr_value(weight, prof)
    if not bmr:
        return 0
    f = ACTIVITY_FACTOR.get((prof or {}).get("activity_level"), 1.2)
    return int(round(bmr * f)) + int(exercise_kcal or 0)


def _hm_to_min(x):
    """HH:MM -> minutes since midnight; None if malformed."""
    try:
        h, m = str(x).strip().split(":")
        h, m = int(h), int(m)
    except Exception:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


def _repair_json(s):
    """Repair almost-JSON that models often write: smart quotes, full-width punctuation, trailing commas,
    // and /* */ comments, Python-style True/False/None, single-quoted strings.
    Only characters outside strings are touched; string contents are left exactly as they are."""
    out = []
    in_str = False
    quote = '"'
    esc = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if esc:
                out.append(ch); esc = False
            elif ch == "\\":
                out.append(ch); esc = True
            elif ch == quote or (quote == "\u201c" and ch == "\u201d"):
                out.append('"'); in_str = False
            elif ch == '"':
                out.append('\\"')          # a straight quote inside a single/smart-quoted string: escape it
            else:
                out.append(ch)
            i += 1
            continue
        if ch == '"' or ch == "'" or ch == "\u201c":
            in_str = True; quote = ch; out.append('"'); i += 1; continue
        if ch == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == "\uff0c":
            out.append(","); i += 1; continue
        if ch == "\uff1a":
            out.append(":"); i += 1; continue
        if ch in "\u3010\u3011":
            out.append("[" if ch == "\u3010" else "]"); i += 1; continue
        out.append(ch); i += 1
    t = "".join(out)
    t = re.sub(r"\bTrue\b", "true", t)
    t = re.sub(r"\bFalse\b", "false", t)
    t = re.sub(r"\bNone\b", "null", t)
    t = re.sub(r",\s*([}\]])", r"\1", t)       # trailing commas
    return t


def _extract_json(text, want=None):
    """Do our best to extract a valid JSON object from a model reply.
    want: top-level key(s) we expect. Reasoning models often restate the JSON template from the prompt
    before the real answer; taking the first {...} gets the template, which has no wanted keys, and the
    user sees "wrong format". With want, every parseable object is collected and the last one with
    a wanted key wins.
    """
    if not text:
        return None
    text = text.strip()
    # <think>...</think> nearly always contains braces; drop it (and an unclosed one at the end)
    text = re.sub(r"(?is)<think>.*?</think>", " ", text)
    text = re.sub(r"(?is)<think>.*$", " ", text).strip()
    if not text:
        return None

    cands = []

    def take(s):
        d = None
        try:
            d = json.loads(s)
        except Exception:
            try:
                d = json.loads(_repair_json(s))
            except Exception:
                return
        if isinstance(d, dict):
            cands.append(d)

    # there may be several ``` blocks; take them all
    for b in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text):
        take(b.strip())
    take(text)

    # Balanced scan from every "{". Braces inside strings don't count, or a "}" in a
    # description would end the block early.
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            break
        depth, end, in_str, esc = 0, -1, False, False
        for k in range(start, len(text)):
            ch = text[k]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        if end < 0:
            break
        take(text[start:end + 1])
        pos = end + 1

    # Truncation rescue: output cut off by max_tokens or the network usually ends mid-object.
    # Cut back to the last complete "}," or "}" and close the open ] and } -- recovers most of it.
    if text.count("{") > text.count("}") or text.count("[") > text.count("]"):
        cut = max(text.rfind("},"), text.rfind("}\n"))
        if cut > 0:
            cand = text[:cut + 1]
            cand += "]" * max(0, cand.count("[") - cand.count("]"))
            cand += "}" * max(0, cand.count("{") - cand.count("}"))
            take(cand)

    if not cands:
        return None
    if want:
        # search from the end: the final answer comes after any reasoning and template. want may be one key or several
        wants = (want,) if isinstance(want, str) else tuple(want)
        for d in reversed(cands):
            for w in wants:
                v = d.get(w)
                if v not in (None, "", [], {}):
                    return d
        for d in reversed(cands):
            if any(w in d for w in wants):
                return d
    return cands[0]



# A unit right after the number ("40 minutes", "3000 steps") means it isn't a body weight
_UNIT_NOT_WEIGHT = r'(?:min\b|mins\b|minutes|hours?|hrs?\b|ml\b|oz\b|ounces|cups?|kcal|cal\b|calories|steps|times|days|weeks|%)'
# "nap goal", "water target" etc. are other goals, not the goal weight
_GOAL_NOT_WEIGHT = re.compile(
    r'\b(nap|sleep|water|hydration|exercise|workout|steps?|calorie|calories|kcal|carbs?|protein|fat|budget|time)\s+(goal|target)', re.I)
_LB_WORD = r'(lb|lbs|pounds?)\b'
_KG_WORD = r'(kg|kgs|kilos?|kilograms?)\b'


def _to_kg(value, unit_text):
    """A number the user typed -> kilograms. Explicit units win; otherwise use their unit preference
    (profile.weight_unit, default pounds)."""
    u = str(unit_text or "").lower()
    if re.match(_KG_WORD, u):
        return value
    if re.match(_LB_WORD, u):
        return round(value / 2.20462, 2)
    pref = ((load_content().get("profile") or {}).get("weight_unit") or "lb")
    return round(value / 2.20462, 2) if pref == "lb" else value


def _plausible_weight(text, match_end, value):
    """Sanity-check an extracted number: wrong unit after it, or outside human body weights -> not a weight.

    Required: without it, "change my nap goal to 40 minutes" once became a goal weight of 40 --
    users with a height set hit the low-weight guard, and users without one had their whole plan
    silently rebuilt around it.
    """
    if value is None:
        return None
    tail = text[match_end:match_end + 10]
    if re.match(r'\s*' + _UNIT_NOT_WEIGHT, tail, re.I):
        return None
    if not (WEIGHT_MIN <= value <= WEIGHT_MAX):
        return None
    return value


def _extract_target_change(text):
    """Pull a new goal weight (kg) out of a request; None if there isn't one."""
    t = str(text or "")
    # clearly a different goal (nap, water, workout, calories...) -> let the model handle it
    if _GOAL_NOT_WEIGHT.search(t):
        return None
    num = r'(\d+(?:\.\d+)?)\s*' + r'((?:lb|lbs|pounds?|kg|kgs|kilos?|kilograms?)\b)?'
    patterns = (
        # from X to Y
        r'\bfrom\s*\d+(?:\.\d+)?\s*(?:lb|lbs|pounds?|kg|kilos?)?\s*to\s*' + num,
        # goal weight (to|is|of|=)? N -- the word "weight" must be there
        r'\b(?:goal|target)\s+weight\s*(?:to|is|of|=|:|at)?\s*' + num,
        # final goal N
        r'\bfinal goal\s*(?:weight)?\s*(?:to|is|of|=|:)?\s*' + num,
    )
    for p in patterns:
        m = re.search(p, t, re.I)
        if m:
            v = _plausible_weight(t, m.end(), _to_kg(float(m.group(1)), m.group(2)))
            if v is not None:
                return v
    # goal/target (to|is) N + a weight unit -- without the word "weight" the unit is required
    m = re.search(r'\b(?:goal|target)\s*(?:to|is|of|=|:)\s*(\d+(?:\.\d+)?)\s*((?:lb|lbs|pounds?|kg|kgs|kilos?|kilograms?)\b)', t, re.I)
    if m:
        v = _plausible_weight(t, m.end(), _to_kg(float(m.group(1)), m.group(2)))
        if v is not None:
            return v
    # get down to / drop to / reach N lb -- unit required
    m = re.search(r'\b(?:get|go|drop|come|be|reach|lose weight)\s*(?:down\s*)?to\s*(\d+(?:\.\d+)?)\s*((?:lb|lbs|pounds?|kg|kgs|kilos?|kilograms?)\b)', t, re.I)
    if m:
        v = _plausible_weight(t, m.end(), _to_kg(float(m.group(1)), m.group(2)))
        if v is not None:
            return v
    return None


def _llm_extract_target(request_text):
    """When the patterns miss, ask the model for just the final goal weight (one number, very stable)."""
    key = ai_configured()
    if not key:
        return None
    prompt = (
        "Is the user asking to change their final goal weight (the weight their plan ends at)?\n"
        "Request: %s\n\n"
        "Rules: if they clearly give a new goal weight, return JSON {\"target\": number, \"unit\": \"lb\" or \"kg\"} "
        "with the unit they used (if they didn't say, \"\"); "
        "if it isn't about the goal weight or there's no clear number, return {\"target\": null}. JSON only, no explanation."
    ) % request_text
    # clearly a different goal (nap, water, workout...) -> no need to ask
    if _GOAL_NOT_WEIGHT.search(str(request_text or "")):
        return None
    text = _call_llm_text(prompt, fast=True)
    d = _extract_json(text)
    if isinstance(d, dict) and isinstance(d.get("target"), (int, float)):
        v = _to_kg(float(d["target"]), d.get("unit") or "")
        # Models misread too. Outside human weights -> treat as not found; better to fall back
        # to normal handling than to rewrite the whole plan.
        if WEIGHT_MIN <= v <= WEIGHT_MAX:
            return v
    return None


def _snapshot_rollback():
    """Snapshot content + data before an AI change, for "Undo"."""
    try:
        snap = {
            "content": load_content(),
            "data": load_data(),
            "time": datetime.datetime.now().isoformat(),
        }
        with open(ROLLBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def rollback_last():
    """Restore content.json + data.json to the snapshot taken before the last AI change."""
    try:
        with open(ROLLBACK_FILE, "r", encoding="utf-8") as f:
            snap = json.load(f)
        ok = False
        if isinstance(snap.get("content"), dict):
            save_content(snap["content"])
            ok = True
        if isinstance(snap.get("data"), dict):
            save_data(snap["data"])
            ok = True
        return ok
    except Exception:
        return False


# ---------- goal guardrails ----------
# Based on common public-health guidance for steady loss; not medical or nutrition advice.
# The app's job is to stop clearly unreasonable input and suggest asking a professional, not to replace one.
SAFE_WEEKLY_LOSS = 1.0     # kg/week (about 2 lb), the usual upper reference for gradual loss
BMI_FLOOR = 18.5           # common reference for underweight (needs height)
WEIGHT_MIN, WEIGHT_MAX = 30.0, 250.0


# BMR (Mifflin-St Jeor) is calculated only in calcBMR() on the page, plus _bmr_value here for the
# server-side summaries -- keep the two formulas identical.


def sanitize_profile(v):
    """Clamp the numeric fields of profile to sane ranges.
    /api/content used to accept anything: the AI-update path had an allow-list, this one didn't,
    so age=999 could be saved and BMR came out absurd. Only fields with physical ranges are
    checked; text and goals pass through.
    """
    if not isinstance(v, dict):
        return {}
    out = dict(v)
    if "sex" in out and out["sex"] not in ("male", "female"):
        out["sex"] = None
    if "activity_level" in out and out["activity_level"] not in ACTIVITY_FACTOR:
        out["activity_level"] = "sedentary"
    if "meal_kcal" in out:
        # Default calories per meal: positive integers up to 5000 only. The page edits these in place;
        # this is the last gate -- a silly value would make the "use default" path log fake numbers.
        v = out["meal_kcal"]
        if isinstance(v, dict):
            clean = {}
            for k, x in v.items():
                try:
                    n = int(float(x))
                    if 0 < n <= 5000 and isinstance(k, str) and len(k) <= 24:
                        clean[k] = n
                except Exception:
                    pass
            out["meal_kcal"] = clean
        else:
            out.pop("meal_kcal")
    if "age" in out:
        try:
            a = int(float(out["age"]))
            out["age"] = a if 10 <= a <= 100 else None
        except Exception:
            out["age"] = None
    if "height_cm" in out:
        try:
            h = float(out["height_cm"])
            if not (100 <= h <= 250):
                out.pop("height_cm")
        except Exception:
            out.pop("height_cm")
    if "protein_g" in out:
        try:
            pg = float(out["protein_g"])
            out["protein_g"] = int(pg) if 20 <= pg <= 300 else None
        except Exception:
            out["protein_g"] = None
    if "bmr_per_kg" in out:
        try:
            k = float(out["bmr_per_kg"])
            if not (10 <= k <= 40):
                out.pop("bmr_per_kg")
        except Exception:
            out.pop("bmr_per_kg")
    if "eating_window" in out:
        out["eating_window"] = sanitize_eating_window(out["eating_window"])
    if "health_note" in out:
        # The user's own health notes (lab numbers, medications). Stored locally; sent with questions when AI is set up.
        out["health_note"] = re.sub(r"\s+", " ", str(out.get("health_note") or "")).strip()[:300]
    return out


# Only three eating windows: 12 hours (12:12), 10 hours (14:10), 8 hours (16:8).
# 8 hours is the floor -- shorter windows (18:6, one meal a day) have less evidence and make enough protein
# hard to reach, so the app doesn't offer them; anything shorter that sneaks in via AI edits or the file is pulled back to 8.
EATING_WINDOW_HOURS = (12, 10, 8)


def _clean_window_core(v, default_start="10:00"):
    try:
        hours = int(float(v.get("hours") or 12))
    except Exception:
        hours = 12
    if hours not in EATING_WINDOW_HOURS:
        hours = min(EATING_WINDOW_HOURS, key=lambda h: abs(h - hours))
    return hours, (clean_hm(v.get("start")) or default_start)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sanitize_eating_window(v):
    """The eating window. Besides the current setting, two more things are stored:
    history -- one entry per change (on, off, new length, new start): {since, enabled, hours, start}.
      Shift workers switch between e.g. 07:30–19:30 and 18:00–06:00; judging a day's meals needs the window
      in force that day. Storing only the current one meant last week's day-shift meals were judged by the night window.
    alt -- a second saved window (the night-shift one, say) to switch to with one click.
    """
    if not isinstance(v, dict):
        return {"enabled": False}
    hours, start = _clean_window_core(v)
    out = {"enabled": bool(v.get("enabled")), "hours": hours, "start": start}
    since = str(v.get("since") or "")
    if _DATE_RE.match(since):
        out["since"] = since
    hist = []
    for h in (v.get("history") or [])[-60:]:
        if not isinstance(h, dict) or not _DATE_RE.match(str(h.get("since") or "")):
            continue
        hh, hs = _clean_window_core(h)
        hist.append({"since": h["since"], "enabled": bool(h.get("enabled")), "hours": hh, "start": hs})
    if hist:
        out["history"] = sorted(hist, key=lambda x: x["since"])
    alt = v.get("alt")
    if isinstance(alt, dict) and clean_hm(alt.get("start")):
        ah, ast = _clean_window_core(alt)
        out["alt"] = {"hours": ah, "start": ast}
    return out


def window_for_date(ew, date_str):
    """Which window applies to a given day: the last one switched on before that day (on the day of a change the
    old one applies -- that day's meals were probably eaten before the change). Without history, the old rule."""
    ew = ew or {}
    hist = ew.get("history") or []
    if hist:
        cur = None
        for h in hist:
            if h["since"] < date_str:
                cur = h
        return cur if (cur and cur.get("enabled")) else None
    if ew.get("enabled") and (not ew.get("since") or ew["since"] < date_str):
        return ew
    return None


def eating_window_text(ew):
    """"10:00–18:00 (16:8)". Empty string if off."""
    if not isinstance(ew, dict) or not ew.get("enabled"):
        return ""
    s = _hm_to_min(ew.get("start"))
    if s is None:
        return ""
    e = (s + int(ew.get("hours") or 12) * 60) % 1440
    return "%s–%02d:%02d (%d:%d)" % (ew["start"], e // 60, e % 60,
                                     24 - int(ew["hours"]), int(ew["hours"]))


def check_goal(start_weight, target, weeks, height_cm=None):
    """Check that a weight-loss goal is in the usual range. Returns (level, msg, suggest_weeks).
    level: "ok" go / "warn" go with a note / "block" refuse to build the plan. Messages show pounds and kilograms.
    """
    try:
        start_weight = float(start_weight)
        target = float(target)
        weeks = int(weeks or 0)
    except Exception:
        return "block", "Start weight, goal weight or number of weeks isn't a valid number", None

    if not (WEIGHT_MIN <= target <= WEIGHT_MAX):
        return ("block", "Goal weight needs to be between %d and %d lb (got %.0f lb)"
                % (WEIGHT_MIN * 2.20462, WEIGHT_MAX * 2.20462, target * 2.20462), None)
    if not (WEIGHT_MIN <= start_weight <= WEIGHT_MAX):
        return "block", "Start weight needs to be between %d and %d lb" % (WEIGHT_MIN * 2.20462, WEIGHT_MAX * 2.20462), None

    # With height known, check the goal against the common underweight line
    if height_cm:
        try:
            h = float(height_cm) / 100.0
            if h > 0.5:
                floor = BMI_FLOOR * h * h
                if target < floor:
                    return ("block",
                            "A goal of %.0f lb (%.1f kg) is below the usual healthy-weight line for your height (about %.0f lb). "
                            "Bodycraft won't build a plan below it -- worth talking to your doctor or a dietitian first."
                            % (target * 2.20462, target, floor * 2.20462),
                            None)
        except Exception:
            pass

    drop = start_weight - target
    if drop <= 0:
        return "ok", "", None          # maintaining or gaining: outside this guard

    if weeks > 0:
        rate = drop / weeks
        if rate > SAFE_WEEKLY_LOSS:
            need = int(-(-drop // SAFE_WEEKLY_LOSS))   # round up
            return ("warn",
                    "Losing %.0f lb in %d weeks means %.1f lb a week, faster than the usual steady pace. "
                    "The plan has been stretched to %d weeks at no more than %.0f lb a week."
                    % (drop * 2.20462, weeks, rate * 2.20462, need, SAFE_WEEKLY_LOSS * 2.20462),
                    need)
    return "ok", "", None


def _regenerate_targets(start_weight, new_target, count):
    """Spread count weekly targets linearly from the start weight to the new goal."""
    targets = []
    for i in range(count):
        if count <= 1:
            w = new_target
        else:
            w = start_weight + (new_target - start_weight) * i / (count - 1)
        targets.append(round(w, 2))
    return targets


def _update_data_targets(new_targets):
    """Write the new weekly targets to every member in data.json."""
    d = load_data()
    members = d.get("members")
    if not isinstance(members, list):
        return
    changed = False
    for m in members:
        if isinstance(m, dict):
            m["targets"] = list(new_targets)
            changed = True
    if changed:
        save_data(d)


def _merge_list_items(current_list, new_list, key, fuzzy=False):
    """Incremental merge: new items update old ones matched by key (fuzzy = by core word), unmatched new items
    are appended, old items stay."""
    cores = ["wake", "breakfast", "lunch", "dinner", "nap", "workout", "walk", "run", "swim",
             "read", "shower", "sleep", "work", "commute", "school", "snack", "stretch", "meeting", "grocer"]

    def _core(t):
        s = str(t or "").lower()
        for c in cores:
            if c in s:
                return c
        return s

    if not isinstance(current_list, list):
            current_list = []          # the field may be null in content.json
    if not isinstance(new_list, list):
        return list(current_list)
    result = list(current_list)
    for ni in new_list:
        if not isinstance(ni, dict):
            continue
        nk = ni.get(key)
        matched = -1
        for i, oi in enumerate(result):
            if not isinstance(oi, dict):
                continue
            ok = oi.get(key)
            if fuzzy:
                if _core(nk) == _core(ok) and str(nk or "").strip() and str(ok or "").strip():
                    matched = i
                    break
            else:
                if nk is not None and nk == ok:
                    matched = i
                    break
        if matched >= 0:
            merged = dict(result[matched])
            merged.update({k: v for k, v in ni.items() if v not in (None, "")})
            result[matched] = merged
        else:
            result.append(ni)
    return result


# Instead of a blanket "no fasting" (which left the model refusing 16:8 while telling people "don't eat after
# dinner" -- which *is* 16:8), eating within a fixed daily window is separated from not eating for long stretches.
# The first is supported with windows of 8+ hours; the second still isn't. Randomized trials find time-restricted
# eating works mainly by eating less, about as well as ordinary calorie reduction, with some signs of lean-mass
# loss -- so it needs protein and strength training, and it isn't magic.
SAFETY_CLAUSE = (
    "[Non-negotiable limits] Keep all advice within steady, sustainable ranges. "
    "Do not suggest very-low-calorie diets, going a full day or more without food, alternate-day fasting, "
    "eating windows shorter than 8 hours, purging, laxatives, or replacing all meals with shakes. "
    "Eating within a fixed 8–12 hour window each day, with only water, black coffee or plain tea outside it "
    "(12:12, 14:10, 16:8 time-restricted eating), can be supported -- but be honest: it mainly helps people eat less "
    "and stop late-night snacking, and works about as well as ordinary eating with the same calories. "
    "Inside the window people still need enough protein and vegetables, and strength training to keep muscle. "
    "It isn't for people who are pregnant or breastfeeding, have a history of eating disorders, take insulin or "
    "glucose-lowering medication, are under 18, are underweight, are being treated for a serious illness, or have "
    "stomach problems -- tell them to check with their doctor first. A shorter window doesn't necessarily mean more loss; "
    "if 12:12 is sustainable, that's fine for good. "
    "Never comment on the user's body or looks, never stoke appearance anxiety, never tie their worth to a number on the scale. "
    "If they express intense fear of eating, repeated loss of control, or clear distress, don't prescribe food -- gently "
    "suggest talking to a doctor or dietitian. After a single big day or a bad mood, acknowledge the feeling first, "
    "say one day changes little, and don't suggest eating less or skipping meals the next day to make up for it.\n"
)

PLAN_PRINCIPLES = (
    SAFETY_CLAUSE +
    "You are an experienced fat-loss coach and home meal planner. When building a plan, follow these principles:\n"
    "1. Reality check: assess the goal honestly. If it's too aggressive (more than about 1% of body weight or 2 lb a week), "
    "adjust it to a steady pace and say why.\n"
    "2. Cook once for the household: one base dinner the whole family eats, with clear differences between the dieter's "
    "plate and everyone else's (half the starch, sauce on the side, extra vegetables instead of bread, etc.).\n"
    "3. Movement in the gaps: fit walks and stretching into the day's natural gaps -- school drop-off, lunch break, "
    "the commute -- and never schedule long, hard workouts that clash with work.\n"
    "4. Sleep and stress: a cut-off for caffeine and heavy meals before bed, a wind-down routine, and a planned "
    "\"bad day\" snack so a rough evening doesn't turn into a binge.\n"
    "5. Grocery list: a weekend shop (proteins, dairy, long-keeping produce) and a midweek top-up (fish, leafy greens), "
    "with specific items.\n"
)


def _refresh_week_plan(weeks, new_target, start_weight, week_count):
    """After a goal change, have the model rewrite each week's focus (ex: movement, dt: food & sleep). On failure, keep the old text."""
    key = ai_configured()
    if not key:
        return
    targets_str = ", ".join("%.0f lb" % (float(wk.get("w")) * 2.20462) for wk in weeks if isinstance(wk, dict) and wk.get("w"))
    prompt = (
        PLAN_PRINCIPLES + "\n\n"
        "The user changed their final goal weight to %.0f lb (start %.0f lb, %d weeks; weekly targets: %s).\n"
        "Following the principles above, rewrite each of the %d weeks' \"movement focus\" (ex) and \"food & routine focus\" (dt).\n"
        'Return JSON: {"weeks":[{"n":1,"ex":"one sentence on movement","dt":"one sentence on food and routine"}, ... %d items ...]}. '

        "ex should fit movement into the day's gaps (drop-off, lunch break, commute); dt should reflect cook-once family dinners "
        "and the sleep/stress plan, and get closer to the final goal week by week. JSON only, no explanation."
    ) % (new_target * 2.20462, start_weight * 2.20462, week_count, targets_str, week_count, week_count)
    text = _call_llm_text(prompt)
    data = _extract_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("weeks"), list):
        return
    by_n = {w.get("n"): w for w in data["weeks"] if isinstance(w, dict)}
    for wk in weeks:
        if not isinstance(wk, dict):
            continue
        nw = by_n.get(wk.get("n"))
        if nw:
            if nw.get("ex"):
                wk["ex"] = nw["ex"]
            if nw.get("dt"):
                wk["dt"] = nw["dt"]


# Only these fields may be supplied by the page as the merge base -- nothing else rides along
BASE_FIELDS = ("menu", "plate", "schedule", "habits", "rules",
               "plate_tips", "reminders", "daily_tips", "profile")


def llm_update_content(request_text, base=None):
    """Update content from a request. Goal-weight changes are handled structurally; everything else goes to the model.
    base is what the page is currently showing, and it's required: the default menu, routine and habits live
    only in the HTML; content.json starts as {}. Without it, "change Monday's dinner" merges into an empty list,
    the result is just Monday, and writing it back wipes the other six default days -- with no error at all.
    """
    # Daily logs and long-term setup are separate pipelines. If "walked 30 minutes" were taken as a habit,
    # the user would get a new habit template and no workout logged. Clear log sentences are refused
    # before the model is called.
    if _looks_like_daily_record(request_text):
        return {"ok": False,
                "err": "That sounds like something to log for today, not a change to your setup. Tell the coach (bottom left) and you'll get a list to confirm before anything is saved.",
                "route": "record"}
    key = ai_configured()
    if not key:
        return {"ok": False, "err": "AI isn't set up yet"}
    current = load_content()
    if isinstance(base, dict):
        for f in BASE_FIELDS:
            if current.get(f):
                continue                      # what's already in content.json wins
            v = base.get(f)
            if isinstance(v, list) and v:
                current[f] = v
            elif isinstance(v, dict) and v:
                merged = dict(v)
                merged.update(current.get(f) or {})
                current[f] = merged

    # ---- step 1: goal-weight changes, structurally (patterns first, a minimal model extraction as fallback) ----
    new_target = _extract_target_change(request_text)
    if new_target is None:
        new_target = _llm_extract_target(request_text)
    profile = current.get("profile") or {}
    start_weight = profile.get("start_weight") or 70.0
    weeks = current.get("weeks") or []
    week_count = len(weeks) or 1
    if new_target is not None and abs(new_target - start_weight) > 0.05:
        level, gmsg, suggest_weeks = check_goal(
            start_weight, new_target, week_count, profile.get("height_cm"))
        if level == "block":
            return {"ok": False, "err": gmsg}
        if level == "warn" and suggest_weeks:
            week_count = suggest_weeks          # slow the pace automatically rather than drawing a steep line
            while len(weeks) < week_count:
                weeks.append({"n": len(weeks) + 1, "d": "Week %d" % (len(weeks) + 1),
                              "ex": "", "dt": ""})
            weeks = weeks[:week_count]
        _snapshot_rollback()  # snapshot for "Undo"
        new_targets = _regenerate_targets(start_weight, new_target, week_count)
        # update weeks[].w in content.json (keep d/ex/dt)
        for i, wk in enumerate(weeks):
            if isinstance(wk, dict) and i < len(new_targets):
                wk["w"] = new_targets[i]
        # have the model rewrite each week's focus (kept as-is on failure)
        _refresh_week_plan(weeks, new_target, start_weight, week_count)
        current["weeks"] = weeks
        save_content(current)
        # sync targets in data.json
        _update_data_targets(new_targets)
        msg = ("Goal weight set to %.0f lb (%.1f kg); %d weekly targets redistributed and each week's focus updated"
               % (new_target * 2.20462, new_target, week_count))
        if level == "warn" and gmsg:
            msg = gmsg + " " + msg
        return {"ok": True, "changed": ["weeks", "targets"], "msg": msg, "notice": gmsg or None}

    # ---- step 2: everything else (routine, menu, plate, habits...) goes to the model ----
    # Pass a numbers-only target series for context
    try:
        d0 = load_data()
        tgt = d0.get("members", [{}])[0].get("targets") or []
        target_summary = ", ".join("%.0f lb" % (float(x) * 2.20462) for x in tgt if x)
    except Exception:
        target_summary = ""
    prompt = (
        # This was missing once. Menus, habits and daily tips are what users see every day,
        # so they need the safety limits more than anything.
        SAFETY_CLAUSE
        + "You generate content for a personal health-routine app. The user describes what to change; you produce JSON for the matching fields.\n\n"
        + knowledge_text().replace("%", "%%") +
        "Current plan, for reference (numbers only): start weight %.0f lb, %d weeks, weekly targets: %s.\n\n"
        "Fields you may produce, with exact formats (fill every field in full like the examples -- **never just a skeleton word**):\n"
        '- "menu" a week of dinners: [{"day":"Mon","base":"Sheet-pan chicken fajitas","baseSub":"chicken thighs + peppers + onions + tortillas","kids":"Family: fajitas with tortillas, cheese and sour cream; lunch box: leftover chicken + rice","me":"5 oz chicken + double peppers in a lettuce wrap, salsa instead of sour cream","tip":"Slice veggies Sunday; 20 min at 425°F"}]\n'

        '- "plate" plate breakdown: [{"day":"Mon","p":"5 oz chicken thigh (~260 kcal)","c":"no tortilla (lettuce wraps)","v":"1½ cups peppers & onions (~70 kcal)","cal":"about 330"}]\n'

        '- "schedule" daily rhythm: [{"t":"06:30","e":"07:00","title":"Wake up & morning routine","type":"life","icon":"🌅","detail":"Glass of water, 3 minutes of stretching","desc":"Same time daily; phone stays on the charger"}]\n'

        '- "habits" daily habits: [{"t":"Glass of water on waking","s":"Rehydrate after the night"}]\n'

        '- "rules" core rules: [{"icon":"😴","title":"Protect your sleep","desc":"Same bedtime on weeknights; screens off 30 min before","pills":["Dark room","Cool room"]}]\n'

        '- "reminders" reminders: [["07:00","Glass of water first"]]\n'

        '- "plate_tips" eyeball tips: [{"icon":"🖐️","title":"5 oz protein","desc":"About the size and thickness of your palm"}]\n'

        '- "daily_tips" tip of the day: {"en":[{"icon":"💧","text":"Aim for 8 cups of water"}]}\n'

        '- "schedule_desc" rhythm caption: "14 blocks, from a 6:30 wake-up to lights out at 10:30"\n'

        '- "profile" goals and copy: {"target_kcal":1600,"water_goal":2000,"exercise_goal":30,"meal_kcal":{"breakfast":350,"lunch":500,"dinner":600},"hero_title":"...","hero_sub":"...","hero_badge":"...","dinner_label":"Your dinner"} (only when changing goals or copy; only the fields being changed; water_goal is in ml)\n\n'

        "Rules:\n"
        "1. Return only the fields the request touches; nothing else\n"
        "2. [Important] Return only the days/items the user mentioned -- **don't return all 7 days for completeness**; unmentioned days would be read as a full overwrite.\n"
        "3. Output strict JSON only: no markdown fences, no explanation\n"
        "4. Match what the user asked for, in the spirit of a steady, healthy routine\n"
        "5. Never produce a weeks field (goal weight is handled separately)\n"
        "6. [Linked] menu and plate describe the same meals. If you change menu, also return the matching plate (calories and ingredients must agree).\n"
        "7. [Detail] every base/baseSub/kids/me/tip and plate p/c/v/cal must be **as detailed as the examples**: "
        "base is a specific dish and method, baseSub the main ingredients, kids the family dinner and lunch box, me your plate with amounts, tip a prep shortcut with time; "
        "p/c/v are specific foods with amounts and estimated calories; cal is their total.\n"
        "**Never skeletal** (base='fish', me='my meal', tip='save time', or plate p='fish', c='veg', v='veg').\n\n"
        "The request:\n%s"
    ) % (start_weight * 2.20462, week_count, target_summary, request_text)
    text = _call_llm_text(prompt)
    if not text:
        return {"ok": False, "err": "The model call failed (no reply)"}
    update = _extract_json(text)
    if not update:
        return {"ok": False, "err": "The model's reply couldn't be read as JSON", "raw": text[:300]}
    # Validate and merge: list fields merge incrementally so a partial reply doesn't wipe the whole array
    list_fields = ["menu", "plate"]
    dict_fields = ["profile"]
    listlist_fields = ["reminders"]
    changed = []

    def _is_thin(item, fields):
        """Is this item a skeleton (any required field empty)?"""
        if not isinstance(item, dict):
            return True
        for f in fields:
            v = item.get(f, "")
            if not isinstance(v, str) or v.strip() == "":
                return True
        return False

    for f in list_fields:
        if f in update and isinstance(update[f], list) and len(update[f]) > 0:
            required = ["base","baseSub","kids","me","tip"] if f == "menu" else ["p","c","v"]
            filtered = [it for it in update[f] if not _is_thin(it, required)]
            if filtered:
                current[f] = _merge_list_items(current.get(f, []), filtered, "day")
                changed.append(f)
    # schedule: fuzzy incremental merge by title
    if "schedule" in update and isinstance(update["schedule"], list) and len(update["schedule"]) > 0:
        current["schedule"] = _merge_list_items(current.get("schedule", []), update["schedule"], "title", fuzzy=True)
        changed.append("schedule")
    # habits: incremental merge by t
    if "habits" in update and isinstance(update["habits"], list) and len(update["habits"]) > 0:
        current["habits"] = _merge_list_items(current.get("habits", []), update["habits"], "t")
        changed.append("habits")
    # rules: incremental merge by title (new titles are appended)
    if "rules" in update and isinstance(update["rules"], list) and len(update["rules"]) > 0:
        current["rules"] = _merge_list_items(current.get("rules", []), update["rules"], "title")
        changed.append("rules")
    # plate_tips: incremental merge by title
    if "plate_tips" in update and isinstance(update["plate_tips"], list) and len(update["plate_tips"]) > 0:
        current["plate_tips"] = _merge_list_items(current.get("plate_tips", []), update["plate_tips"], "title")
        changed.append("plate_tips")
    # daily_tips
    if "daily_tips" in update and isinstance(update["daily_tips"], dict) and update["daily_tips"].get("en"):
        current["daily_tips"] = update["daily_tips"]
        changed.append("daily_tips")
    # schedule_desc
    if "schedule_desc" in update and isinstance(update["schedule_desc"], str) and update["schedule_desc"].strip():
        current["schedule_desc"] = update["schedule_desc"]
        changed.append("schedule_desc")
    for f in dict_fields:
        if f in update and isinstance(update[f], dict):
            if f == "profile":
                # accept valid values only; reject literals like "YYYY-MM-DD"
                target = current.setdefault("profile", {})
                for k, v in update[f].items():
                    if k in ("start_date", "end_date"):
                        if isinstance(v, str) and re.match(r'^\d{4}-\d{2}-\d{2}$', v):
                            target[k] = v
                    elif k == "start_weight":
                        if isinstance(v, (int, float)) and 30 <= v <= 250:
                            target[k] = v
                    elif k in ("target_kcal", "water_goal", "exercise_goal"):
                        if isinstance(v, (int, float)) and 0 < v <= 100000:
                            target[k] = v
                    elif k == "weight_unit":
                        if v in ("lb", "kg"):
                            target[k] = v
                    elif k == "meal_kcal":
                        if isinstance(v, dict):
                            mk = target.setdefault("meal_kcal", {})
                            for meal in ("breakfast", "lunch", "dinner", "snack"):
                                if isinstance(v.get(meal), (int, float)) and 0 < v[meal] <= 5000:
                                    mk[meal] = v[meal]
                    elif k == "activity_level":
                        if v in ACTIVITY_FACTOR:
                            target["activity_level"] = v
                    elif k == "sex":
                        # clearing is allowed (set once, then changed to "rather not say")
                        if v in ("male", "female", None, ""):
                            target["sex"] = v or None
                    elif k == "age":
                        if v is None or v == "":
                            target["age"] = None
                        elif isinstance(v, (int, float)) and 10 <= v <= 100:
                            target["age"] = int(v)
                    elif k == "height_cm":
                        if isinstance(v, (int, float)) and 100 <= v <= 250:
                            target["height_cm"] = v
                    elif k == "bmr_per_kg":
                        if isinstance(v, (int, float)) and 10 <= v <= 40:
                            target["bmr_per_kg"] = v
                    elif k in ("hero_title", "hero_sub", "hero_badge", "dinner_label"):
                        if isinstance(v, str) and v.strip():
                            target[k] = v
                changed.append(f)
            else:
                current.setdefault(f, {}).update(update[f])
                changed.append(f)
    for f in listlist_fields:
        if f in update and isinstance(update[f], list):
            valid = [x for x in update[f] if isinstance(x, list) and len(x) >= 2]
            if valid:
                current[f] = valid
                changed.append(f)
    if not changed:
        return {"ok": False,
                "err": "Couldn't tell what to change. Being specific helps, e.g. "
                       "\"move wake-up to 6:30\", \"swap Wednesday's dinner for baked salmon\", \"add a habit: 10-minute walk after lunch\".",
                "raw": text[:300]}
    # Linked fallback: menu changed but plate didn't -> generate plate so calories match the menu
    if "menu" in changed and "plate" not in changed:
        try:
            plate_prompt = (
                "From this week of dinners, produce the matching plate breakdown (**every item in full detail**).\n"
                "Menu: %s\n\n"
                'Return JSON: {"plate":['

                '{"day":"Mon","p":"5 oz sirloin (~250 kcal)","c":"no starch","v":"2 cups tomato, red cabbage & lettuce (~80 kcal)","cal":"about 330"},'

                '... 7 items ...'

                ']}\n'
                "Each item: p/c/v name specific foods with amounts and estimated calories (in parentheses); cal is their total. "
                "**Never skeletal** (p='fish', c='veg', v='veg', cal='about 300 kcal'). "
                "JSON only, no explanation."
            ) % json.dumps(current.get("menu", []), ensure_ascii=False)
            plate_text = _call_llm_text(plate_prompt)
            plate_update = _extract_json(plate_text)
            if isinstance(plate_update, dict) and isinstance(plate_update.get("plate"), list) and len(plate_update["plate"]) > 0:
                current["plate"] = _merge_list_items(current.get("plate", []), plate_update["plate"], "day")
                changed.append("plate")
        except Exception:
            pass
    _snapshot_rollback()  # snapshot for "Undo"
    save_content(current)
    return {"ok": True, "changed": changed, "content": current}


# ============ "who is this person", for the model ============
# Found while testing: asked "is 16:8 right for me?", the coach's prompt had nothing personal in it --
# no sex, age, goal, days in, eating window -- so it could only give generic answers.
# It's all in local files; the server builds it itself rather than relying on the page (whose profile can arrive late).

def _w(kg):
    """A body weight for the model, in the user's own unit (lb by default), with kg alongside."""
    try:
        kg = float(kg)
    except Exception:
        return str(kg)
    pref = ((load_content().get("profile") or {}).get("weight_unit") or "lb")
    if pref == "kg":
        return "%.1f kg" % kg
    return "%.1f lb (%.1f kg)" % (kg * 2.20462, kg)


def _member_and_profile(member=None):
    data = load_data()
    mid = safe_member(member or data.get("active") or "default")
    mem = next((x for x in (data.get("members") or []) if x.get("id") == mid), None) \
        or ((data.get("members") or [{}])[0])
    prof = dict(load_content().get("profile") or {})
    ov = (mem.get("overrides") or {}).get("profile") if isinstance(mem.get("overrides"), dict) else None
    if isinstance(ov, dict):
        for k in ("start_weight", "start_date", "end_date"):
            if ov.get(k):
                prof[k] = ov[k]
    # For members other than the main user, their own height, sex, age and activity win -- same rule as bodyOf()
    # on the page. Previously the whole household's BMR used the main profile.
    if mid != "default" and isinstance(mem, dict):
        for k in ("height_cm", "sex", "age", "activity_level", "start_weight"):
            if mem.get(k) not in (None, "", 0):
                prof[k] = mem[k]
    return mid, mem or {}, prof


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def week_averages(actual, today=None):
    """Average weight over the last 7 days and the 7 before (at least 3 weigh-ins each).
    Day-to-day weight swings with water, salt, hormones and digestion -- a couple of pounds is normal.
    Comparing two 7-day averages shows where it's really heading; that's also what the app teaches.
    """
    today = today or datetime.date.today()
    def window(a, b):
        out = []
        for k, v in (actual or {}).items():
            try:
                d = datetime.date.fromisoformat(k)
            except Exception:
                continue
            if a <= (today - d).days <= b and v is not None:
                out.append(float(v))
        return out
    cur, prev = window(0, 6), window(7, 13)
    return (_avg(cur) if len(cur) >= 3 else None, _avg(prev) if len(prev) >= 3 else None)


def user_brief(member=None):
    mid, mem, prof = _member_and_profile(member)
    bits = []
    who = [str(mem.get("name") or "").strip() or "the user"]
    if prof.get("sex") in ("female", "male"):
        who.append("female" if prof["sex"] == "female" else "male")
    if prof.get("age"):
        who.append("%s years old" % prof["age"])
    if prof.get("height_cm"):
        h = float(prof["height_cm"]); who.append("height %d'%d\" (%s cm)" % (int(h / 2.54 // 12), int(round(h / 2.54 % 12)), prof["height_cm"]))
    bits.append("Name and basics: " + ", ".join(who))
    hn = str(prof.get("health_note") or "").strip()
    if hn:
        bits.append("Health notes they wrote themselves: " + hn[:300])
    sw = mem.get("start_weight") or prof.get("start_weight")
    tg = (mem.get("targets") or [None])[-1]
    sd = prof.get("start_date")
    line = []
    if sw:
        line.append("started at %s" % _w(sw))
    if sd:
        try:
            n = (datetime.date.today() - datetime.date.fromisoformat(sd)).days
            line.append("began %s; today is day %d (the start day is day 1; the start weight was entered that day, not necessarily a morning weigh-in)"
                        % (sd, max(0, n) + 1))
        except Exception:
            pass
    if tg:
        line.append("goal %s" % _w(tg))
    if sw:
        try:
            line.append("losing the first 5%% (about %s, down to %s) usually improves blood sugar, triglycerides, blood pressure and liver fat"
                        % (_w(float(sw) * 0.05), _w(float(sw) * 0.95)))
        except Exception:
            pass
    if line:
        bits.append("Plan: " + "; ".join(line))
    actual = mem.get("actual") or {}
    if actual:
        last_d = max(actual.keys())
        bits.append("Last weigh-in: %s (%s)" % (_w(actual[last_d]), last_d))
        cur, prev = week_averages(actual)
        if cur is not None and prev is not None:
            bits.append("Last 7-day average %s, previous 7 days %s (change %+.1f lb)" % (_w(cur), _w(prev), (cur - prev) * 2.20462))
        elif cur is not None:
            bits.append("Last 7-day average %s (less than two weeks of weigh-ins, no trend yet)" % _w(cur))
    return "\n".join(bits)


def meal_macros(info):
    """Protein/carbs/fat for one meal. Only when every food has macros; any missing -> None."""
    foods = (info or {}).get("foods") if isinstance(info, dict) else None
    if not isinstance(foods, list) or not foods:
        return None
    tot = {"p": 0.0, "c": 0.0, "f": 0.0}
    for f in foods:
        mac = macro_fields(f)
        if not mac:
            return None
        for k in tot:
            tot[k] += mac[k]
    return {k: round(v, 1) for k, v in tot.items()}


def day_macros(meta_day):
    """Macros for a day: only meals logged with foods count. Returns (totals, meals with detail, meals logged)."""
    tot = {"p": 0.0, "c": 0.0, "f": 0.0}
    n_known = n_all = 0
    for mk, info in (meta_day or {}).items():
        if not isinstance(info, dict) or info.get("cal") in (None, ""):
            continue
        n_all += 1
        mac = meal_macros(info)
        if mac:
            n_known += 1
            for k in tot:
                tot[k] += mac[k]
    return {k: round(v) for k, v in tot.items()}, n_known, n_all


def protein_target(prof, weight):
    """Daily protein target while losing fat: 1.5 g per kg of body weight (about 0.7 g/lb; common guidance is 1.2–1.6 g/kg).
    If the user set their own under Goals, use that."""
    try:
        pg = float((prof or {}).get("protein_g") or 0)
        if 20 <= pg <= 300:
            return int(pg)
    except Exception:
        pass
    try:
        w = float(weight or 0)
    except Exception:
        w = 0
    return int(round(w * 1.5)) if w > 0 else 0


def _meal_times(meta_day):
    """Meals with a time on a day: [(HH:MM, meal name, kcal)], sorted by time."""
    out = []
    for k, v in (meta_day or {}).items():
        if isinstance(v, dict) and clean_hm(v.get("at")) and (v.get("cal") not in (None, "")):
            out.append((clean_hm(v["at"]), meal_label(k), v.get("cal")))
    return sorted(out)


def in_window(hm, ew):
    """Is a time of day inside the eating window (windows crossing midnight included)?"""
    s, t = _hm_to_min(ew.get("start")), _hm_to_min(hm)
    if s is None or t is None:
        return None
    e = s + int(ew.get("hours") or 12) * 60
    return (s <= t <= e) or (e > 1440 and t <= e - 1440)


def eating_brief(member=None, now=None):
    mid, mem, prof = _member_and_profile(member)
    ew = sanitize_eating_window(prof.get("eating_window") or {})
    meta = (load_photo_meta() or {}).get(mid) or {}
    now = now or datetime.datetime.now()
    today = now.date()
    lines = []
    wt = eating_window_text(ew)
    if wt:
        lines.append("The user chose an eating window: %s%s. Outside it, only water, black coffee or plain tea."
                     % (wt, (", since %s" % ew["since"]) if ew.get("since") else ""))
        if ew.get("alt"):
            lines.append("They also saved a second window, %s (for switching shifts)."
                         % eating_window_text(dict(ew["alt"], enabled=True)))
    else:
        lines.append("The user has no eating window on (not doing 16:8 or similar time-restricted eating).")
    # When was the last bite -> how long fasting
    last = None
    for back in (0, 1):
        d = today - datetime.timedelta(days=back)
        ts = _meal_times(meta.get(d.isoformat()))
        if ts:
            hh, mm = map(int, ts[-1][0].split(":"))
            last = datetime.datetime(d.year, d.month, d.day, hh, mm)
            if last <= now:
                break
            last = None
    tt = _meal_times(meta.get(today.isoformat()))
    if tt:
        lines.append("Meal times today: " + ", ".join("%s %s %s kcal" % x for x in tt))
    # Enough protein? The item most often missed while cutting -- and where muscle loss starts
    mac, n_known, n_all = day_macros(meta.get(today.isoformat()))
    if n_known:
        wnow = None
        for d_ in sorted((mem.get("actual") or {}).keys(), reverse=True):
            if (mem.get("actual") or {}).get(d_):
                wnow = (mem.get("actual") or {}).get(d_)
                break
        tgt = protein_target(prof, wnow or prof.get("start_weight"))
        lines.append("In the %d meal(s) logged with foods today: protein %d g%s, carbs %d g, fat %d g%s."
                     % (n_known, mac["p"], (" (target about %d g, 1.5 g per kg / 0.7 g per lb)" % tgt) if tgt else "",
                        mac["c"], mac["f"], ("; %d more meal(s) logged without foods aren't counted" % (n_all - n_known)) if n_all > n_known else ""))
    if last:
        h = (now - last).total_seconds() / 3600
        lines.append("Last ate at %s; it's now %s, about %.1f hours ago."
                     % (last.strftime("%m-%d %H:%M"), now.strftime("%H:%M"), h))
    if ew.get("enabled"):
        ok = tot = 0
        outside = []
        for back in range(1, 8):
            d = today - datetime.timedelta(days=back)
            wd = window_for_date(ew, d.isoformat())   # the window in force that day (shift workers switch)
            if not wd:
                continue
            ts = _meal_times(meta.get(d.isoformat()))
            if not ts:
                continue
            tot += 1
            bad = [x for x in ts if in_window(x[0], wd) is False]
            if bad:
                outside.append("%s %s" % (d.strftime("%m/%d"), ", ".join("%s %s" % (b[0], b[1]) for b in bad)))
            else:
                ok += 1
        if tot:
            lines.append("In the last 7 days, %d day(s) have meal times logged; on %d of them everything was eaten inside the window." % (tot, ok)
                         + ((" Eaten outside the window: " + "; ".join(outside[:4]) + ".") if outside else ""))
    return "\n".join(lines)


# ============ the coach ============
PERSONAS = {
    "warm": {
        "name": "Rosie",
        "intro": "a kind, patient personal coach",
        "style": ("You are Rosie, the user's personal health coach: kind, knowledgeable, never blaming. "
                  "You notice good changes in their data that they missed, and when they're discouraged you encourage them sincerely. "
                  "No empty motivational fluff -- every bit of encouragement rests on something specific in their data. "
                  "Keep replies short (2–4 sentences), conversational, like a friend talking. No bullet points, no lectures."),
    },
    "roast": {
        "name": "Mack",
        "intro": "a blunt, funny friend who calls out excuses",
        "style": ("You are Mack, the user's tough-love friend: blunt, direct, quick to poke holes in excuses, but always on their side. "
                  "You use humor and sharp observations to make the problem obvious, like \"Bed at 1am and you're planning to walk it off?\" "
                  "Hard limits: never mock their body, never shame, never stoke appearance anxiety. "
                  "Keep replies short (2–4 sentences): one jab plus one honest point. No bullet points, no lectures."),
    },
    "analyst": {
        "name": "Data",
        "intro": "a calm analyst who sticks to the numbers",
        "style": ("You are Data, the user's health data analyst: calm and objective, facts and numbers only, no emotion, no judgment. "
                  "You pull trends, swings and outliers out of the data and give precise quantitative insight. "
                  "Keep replies short (2–4 sentences); let the numbers talk, with at most one key quantitative takeaway. No bullet points, no lectures."),
    },
}


def coach_reply(persona, events, context, history=None):
    """One reply from the coach: persona + triggering events + data context. history is a multi-turn conversation."""
    key = ai_configured()
    if not key:
        return None
    p = PERSONAS.get(persona) or PERSONAS["warm"]
    style = SAFETY_CLAUSE + p["style"]
    # assemble the data (keep it bounded)
    weights = context.get("weights") or {}
    meals = context.get("meals") or {}
    diary = context.get("diary") or ""
    events_txt = ""
    for e in events:
        events_txt += "- %s: %s\n" % (e.get("type") or "", e.get("detail") or "")
    weights_txt = ""
    if isinstance(weights, dict) and weights:
        ws = sorted(weights.items())
        weights_txt = ", ".join("%s=%s" % (k, _w(v)) for k, v in ws[-14:])
    meals_txt = ""
    if isinstance(meals, dict) and meals:
        # Not hard-coded to three meals -- snacks and custom meals count too.
        # The page sends them in its own order with display names.
        # What was eaten matters too: a calorie number alone can't answer "did I get enough protein?"
        names = dict(MEAL_NAMES)
        for mk, m in meals.items():
            if not (m and m.get("cal")):
                continue
            nm = m.get("name") or names.get(mk, mk)
            items = str(m.get("items") or "").strip()
            src = " (default estimate)" if m.get("source") == "default" else ""
            at = clean_hm(m.get("at"))
            mac = ""
            if m.get("p") is not None:
                try:
                    mac = ", protein %s g" % int(round(float(m.get("p"))))
                except Exception:
                    mac = ""
            meals_txt += "%s%s %s kcal%s%s%s; " % (nm, (" " + at) if at else "", m.get("cal"),
                                                (", " + items[:80]) if items else "", mac, src)
        total = sum(int(m.get("cal") or 0) for m in meals.values() if isinstance(m, dict))
        if total:
            meals_txt += "Total %d kcal." % total
        # protein total (meals logged with foods only) + target, so the coach has a reference
        tp = [float(m.get("p")) for m in meals.values() if isinstance(m, dict) and m.get("p") is not None]
        mt = context.get("macro_targets") if isinstance(context, dict) else None
        if tp and isinstance(mt, dict) and mt.get("p"):
            meals_txt += " Protein in meals logged with foods: %d g; today's target about %s g." % (int(round(sum(tp))), mt.get("p"))
    diary_txt = diary[:200] if diary else ""
    exercise = context.get("exercise") or {}
    sleep = context.get("sleep") or {}
    ex_txt = ""
        # Several workouts a day (a list). Old code expected one object -- after the switch to lists the coach
        # couldn't see any exercise and told people they hadn't moved. Both shapes are accepted.
    ex_list = exercise if isinstance(exercise, list) else ([exercise] if isinstance(exercise, dict) and exercise.get("type") else [])
    parts = []
    for e in ex_list:
        if isinstance(e, dict) and e.get("type"):
            step_txt = (", %s steps" % e.get("steps")) if e.get("steps") else ""
            parts.append("%s %s min%s%s, about %s kcal" % (e.get("type"), e.get("duration"), step_txt,
                         (" (" + e["intensity"] + ")") if e.get("intensity") else "", e.get("kcal", "?")))
    if parts:
        ex_txt = "; ".join(parts)
        if len(parts) > 1:
            ex_txt += "; %d minutes total" % sum(int(e.get("duration") or 0) for e in ex_list if isinstance(e, dict))
    sl_txt = ""
    if isinstance(sleep, dict) and sleep.get("bed"):
        sl_txt = "%s→%s, quality %s/5" % (sleep.get("bed"), sleep.get("wake"), sleep.get("quality", "?"))

    member = context.get("member") if isinstance(context, dict) else None
    try:
        brief = user_brief(member)
    except Exception:
        brief = ""
    try:
        eat = eating_brief(member)
    except Exception:
        eat = ""
    last_user = ""
    for m in reversed(history or []):
        role = m.get("role") if isinstance(m, dict) else (m[0] if isinstance(m, (list, tuple)) and m else "")
        if role == "user":
            last_user = (m.get("content") if isinstance(m, dict) else m[1]) or ""
            break
    kind = distress_kind(last_user)
    if not kind and any((e or {}).get("type") == "distress" for e in (events or [])):
        kind = "lapse"
    # The personas say "2–4 sentences". Fine for small talk, but "is this right for me?" or "why isn't it moving?"
    # can't be answered that briefly. Allow a little more for knowledge questions.
    length_rule = ("Usually 2–4 sentences as the persona says; if the user asks a knowledge question (is it right for me, why, how), "
                   "up to 6 sentences to cover the key conditions -- still no bullet points.\n")
    moment = ""
    if kind == "craving":
        moment = ("[Right now] The user is craving something and hasn't eaten it yet. First say, in one sentence, that this is normal. "
                  "Then use the meal-time data above: when they last ate, how long they've been fasting, when the window opens (if they use one). "
                  "Offer one or two things to do right now: a hot drink or herbal tea, brush their teeth, leave the kitchen, wait ten minutes and then decide. "
                  "If dinner was too small and they're truly hungry, a small protein snack (Greek yogurt, an egg) is fine -- no guilt. "
                  "If they mention a racing heart, shakiness, dizziness or cold sweats, tell them to eat something now regardless of the window "
                  "and check with a doctor later. No lecturing.\n")
    elif kind == "worry":
        moment = ("[Right now] The user is anxious about the number on the scale. Acknowledge it in one sentence, then explain using their own data: "
                  "daily weight moves with water, salt, carbs, hormones, digestion and the time of weigh-in -- two or three pounds up or down in a day is common; "
                  "fast loss in the first two weeks and slower after is normal; compare the last 7-day average with the 7 days before. "
                  "If the data shows a specific cause (a salty dinner, eating outside the window, a start weight taken in the evening), point it out. "
                  "No \"just keep going\" platitudes, and don't suggest eating less.\n")
    elif kind == "lapse":
        moment = ("[Right now] The user is down or beating themselves up. Acknowledge the feeling in a sentence or two. One big day, or a few days "
                  "without progress, isn't failure -- many people quit on the thought \"well, I've blown it now\"; one big day doesn't undo the work before it. "
                  "What they just described probably isn't logged yet; go by what they said. "
                  "If the data shows a trigger (skipped dinner, a long fast, too little sleep), gently point it out with one way to head it off next time. "
                  "Offer at most one small next step, like weighing in as usual tomorrow and eating breakfast as usual -- no skipping meals to make up. "
                  "Don't count calories, don't list what they ate, don't suggest eating less or not eating. Only if they say this keeps happening and "
                  "they can't stop, suggest a doctor or dietitian.\n")
    sys_prompt = (
        style + length_rule + "\n"
        + knowledge_text(member or "default")
        + (("About this person:\n" + brief + "\n\n") if brief else "")
        + (("Meal timing:\n" + eat + "\n\n") if eat else "")
        + moment +
        "What the user logged:\n"
        "Triggering events:\n" + (events_txt or "(none)") + "\n"
        "Recent weigh-ins (date=weight):\n" + (weights_txt or "(none yet)") + "\n"
        "Today's meals:\n" + (meals_txt or "(none yet)") + "\n"
        "Today's exercise:\n" + (ex_txt or "(none yet)") + "\n"
        "Last night's sleep:\n" + (sl_txt or "(none yet)") + "\n"
        "Today's journal:\n" + (diary_txt or "(none yet)") + "\n\n"
        "Say a few sentences based on all of this. Output only what you'd say -- no prefix, label or markdown."
    )
    if history:
        messages = [{"role": "system", "content": sys_prompt}]
        for m in history[-8:]:
            if isinstance(m, dict):
                messages.append({"role": m.get("role") or "user", "content": m.get("content") or ""})
            elif isinstance(m, (list, tuple)) and len(m) >= 2:
                messages.append({"role": m[0], "content": m[1]})
    else:
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": "Just logged that -- any thoughts?"}]
    return _call_llm_messages(messages)


def collect_period(member, start, end, include_diary=True):
    """Gather everything in a date range as material, each item dated.
    The first weekly review only covered "this week" and fed the model four totals (weight change, exercise
    minutes, sleep, habit rate) -- no journal, no meal detail, no photos -- so it could only say generic things.
    This collects everything available for the period.
    """
    m = safe_member(member)
    data = load_data()
    mem = None
    for x in (data.get("members") or []):
        if x.get("id") == m:
            mem = x
            break
    mem = mem or {}

    def in_range(d):
        return isinstance(d, str) and len(d) == 10 and start <= d <= end

    days = {}

    def slot(d):
        return days.setdefault(d, {"date": d})

    for d, v in (mem.get("actual") or {}).items():
        if in_range(d) and v is not None:
            slot(d)["weight"] = v
    # several workouts a day; old data was a single object, so wrap it
    for d, v in (mem.get("exercises") or {}).items():
        if not in_range(d) or not v:
            continue
        arr = v if isinstance(v, list) else [v]
        got = [{"type": x.get("type"), "duration": x.get("duration"),
                "intensity": x.get("intensity"), "kcal": x.get("kcal")}
               for x in arr if isinstance(x, dict)]
        if got:
            slot(d)["exercises"] = got
            # kept for older code that reads one
            slot(d)["exercise"] = got[0]
    for d, v in (mem.get("sleep") or {}).items():
        if in_range(d) and isinstance(v, dict):
            slot(d)["sleep"] = {"bed": v.get("bed"), "wake": v.get("wake"),
                                "quality": v.get("quality")}

    # Meals are stored member -> date -> meal.
    # Reading them date -> member finds nothing and the preview says "0 days of meals" --
    # a wrong nesting level doesn't error, it silently reads empty, so check against a real file.
    for d, meals in ((load_photo_meta() or {}).get(m) or {}).items():
        if not in_range(d):
            continue
        got = {}
        for mk, info in (meals or {}).items():
            if isinstance(info, dict) and info.get("cal") is not None:
                got[mk] = {"cal": info.get("cal"), "items": info.get("items", "")}
                if clean_hm(info.get("at")):
                    got[mk]["at"] = clean_hm(info.get("at"))
        if got:
            slot(d)["meals"] = got
            mac, n_known, n_all = day_macros(meals)
            if n_known:
                slot(d)["macros"] = dict(mac, known=n_known, all=n_all)

    if include_diary:
        # also member -> date
        for d, txt in ((load_diary() or {}).get(m) or {}).items():
            if not in_range(d):
                continue
            if isinstance(txt, str) and txt.strip():
                slot(d)["diary"] = txt.strip()[:400]
            elif isinstance(txt, dict) and str(txt.get("text") or "").strip():
                slot(d)["diary"] = str(txt["text"]).strip()[:400]

    selfie_n = 0
    for d, items in ((load_selfies() or {}).get(m) or {}).items():
        if in_range(d) and items:
            slot(d)["selfies"] = len(items)
            selfie_n += len(items)

    ordered = [days[k] for k in sorted(days.keys())]
    ws = [x["weight"] for x in ordered if x.get("weight") is not None]
    ex = []
    for x in ordered:
        ex.extend(x.get("exercises") or ([x["exercise"]] if x.get("exercise") else []))
    total_days = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 1
    return {
        "start": start, "end": end, "total_days": total_days,
        "days": ordered,
        "recorded_days": len(ordered),
        "weight_first": ws[0] if ws else None,
        "weight_last": ws[-1] if ws else None,
        "weight_change": (round(ws[-1] - ws[0], 1) if len(ws) >= 2 else None),
        "weight_count": len(ws),
        "exercise_count": len(ex),
        "exercise_minutes": sum(int(e.get("duration") or 0) for e in ex),
        "exercise_kcal": sum(int(e.get("kcal") or 0) for e in ex),
        "diary_count": sum(1 for x in ordered if x.get("diary")),
        "meal_days": sum(1 for x in ordered if x.get("meals")),
        "selfie_count": selfie_n,
    }


_SMALL = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def en(n):
    """Small numbers as words ("six of seven days"), larger ones as digits -- the way people write."""
    try:
        n = int(n)
    except Exception:
        return str(n)
    return _SMALL[n] if 0 <= n <= 10 else str(n)


def md_day(d):
    """2026-09-03 -> Sep 3. A year on every date reads stiffly in a note to yourself."""
    try:
        return datetime.date.fromisoformat(d).strftime("%b %-d") if os.name != "nt" else \
            datetime.date.fromisoformat(d).strftime("%b %#d")
    except Exception:
        return d


def period_badges(period):
    """The badges earned in a period.

    Almost all of them reward showing up and paying attention, not pounds lost -- weight swings with water,
    hormones and weigh-in timing, and making it the main reward turns it into pressure. Logging, coming back
    after a break, writing down how the day felt: those are earned. Worded like a friend talking, not a certificate.
    """
    days = period.get("days") or []
    dates = sorted(x["date"] for x in days)
    rec = len(dates)
    total = period.get("total_days") or 1
    out = []

    def add(icon, name, why, tier="normal"):
        out.append({"icon": icon, "name": name, "why": why, "tier": tier})

    if rec >= 1:
        add("🌱", "Day one", "%s: your first entry" % md_day(dates[0]))
    ratio = rec / total
    if rec >= 3:
        add("📔", "%s days here" % en(rec).capitalize(), "%s of %s days have something written down" % (en(rec).capitalize(), en(total)))
    if ratio >= 0.8 and rec >= 5:
        add("🌟", "Hardly missed a day", "only %s of %s days without an entry" % (en(total - rec), en(total)), "gold")
    elif ratio >= 0.5 and rec >= 4:
        add("🍀", "More days than not", "you showed up on over half the days")

    best = cur = 0
    prev = None
    for d in dates:
        cd = datetime.date.fromisoformat(d)
        cur = cur + 1 if (prev and (cd - prev).days == 1) else 1
        best = max(best, cur)
        prev = cd
    if best >= 3:
        add("🔥", "%s days in a row" % en(best).capitalize(), "not a single gap",
            "gold" if best >= 7 else "normal")

    gaps = 0
    for i in range(1, len(dates)):
        gap = (datetime.date.fromisoformat(dates[i]) - datetime.date.fromisoformat(dates[i-1])).days
        if gap >= 2:
            gaps += 1
    if gaps >= 1 and rec >= 4:
        add("🕊️", "Came back",
            "you stopped %s and picked it up again. That's harder than never stopping" % ("once" if gaps == 1 else "%s times" % en(gaps)), "gold")

    exn = period.get("exercise_count") or 0
    exm = period.get("exercise_minutes") or 0
    if exn >= 1:
        add("👟", "Moved %s" % ("once" if exn == 1 else "%s times" % en(exn)), "%d minutes in all" % exm)
    if exm >= 150:
        add("💪", "%d minutes" % exm, "time you spent on your body in this stretch",
            "gold" if exm >= 300 else "normal")
    early = sum(1 for x in days if (x.get("sleep") or {}).get("bed", "")
                and ("21:" <= x["sleep"]["bed"] <= "23:30"))
    if early >= 2:
        add("🌙", "%s early nights" % en(early).capitalize(), "in bed by 11:30")

    dn = period.get("diary_count") or 0
    if dn >= 1:
        add("✍️", "Wrote it down %s" % ("once" if dn == 1 else "%s times" % en(dn)), "what was on your mind that day, kept",
            "gold" if dn >= 5 else "normal")
    sn = period.get("selfie_count") or 0
    if sn >= 1:
        add("📷", "%s progress photo%s" % (en(sn).capitalize(), "" if sn == 1 else "s"), "one day you'll be glad you took them")
    mn = period.get("meal_days") or 0
    if mn >= 3:
        add("🍽️", "%s days of meals logged" % en(mn).capitalize(), "you know what you're eating")

    wc = period.get("weight_change")
    if wc is not None and wc <= -0.3:
        add("🍃", "Down %s" % _w(abs(wc)).split(" (")[0],
            "from %s to %s" % (_w(period.get("weight_first")).split(" (")[0], _w(period.get("weight_last")).split(" (")[0]))
    elif wc is not None and abs(wc) < 0.3:
        add("⚖️", "Steady", "barely moved -- holding steady is its own skill")
    wn = period.get("weight_count") or 0
    if wn >= 5:
        add("👀", "%s weigh-ins" % en(wn).capitalize(), "looking at the number is how it starts to move")

    return out


def review_text(period, persona="warm", note=""):
    """Have the model write the review from the material. Returns markdown-ish text; the page lays it out."""
    if not ai_configured():
        return {"ok": False, "err": "AI isn't set up yet"}
    lines = []
    for d in period["days"]:
        bits = ["%s:" % d["date"]]
        if d.get("weight") is not None:
            bits.append("weight %s" % _w(d["weight"]))
        for e in (d.get("exercises") or []):
            bits.append("exercise %s %s min (about %s kcal)" % (e.get("type"), e.get("duration"), e.get("kcal")))
        if d.get("sleep"):
            sl = d["sleep"]
            bits.append("sleep %s→%s, quality %s/5" % (sl.get("bed"), sl.get("wake"), sl.get("quality")))
        if d.get("meals"):
            # sorted by time eaten, untimed last. Display names, not keys -- a key would be copied into the review.
            ms = sorted(d["meals"].items(), key=lambda kv: kv[1].get("at") or "99")
            mt = "; ".join("%s%s %s kcal%s" % (meal_label(k), (" " + v["at"]) if v.get("at") else "",
                                              v.get("cal"), (" (%s)" % v["items"]) if v.get("items") else "")
                          for k, v in ms)
            bits.append("meals: " + mt)
            mc = d.get("macros")
            if mc:
                bits.append("protein %d g, carbs %d g, fat %d g (%d/%d meals logged with foods)" % (mc["p"], mc["c"], mc["f"], mc["known"], mc["all"]))
        if d.get("diary"):
            bits.append("journal: " + d["diary"])
        lines.append(" ".join(bits))
    detail = "\n".join(lines) or "(nothing logged in this period)"
    p_ = PERSONAS.get(persona) or PERSONAS["warm"]
    try:
        brief = user_brief(period.get("member"))
        ew = sanitize_eating_window((_member_and_profile(period.get("member"))[2]).get("eating_window") or {})
        wt = eating_window_text(ew)
    except Exception:
        brief, wt = "", ""
    about = ""
    if brief:
        about += "About this person:\n" + brief + "\n"
    if wt:
        about += ("They chose an eating window of %s. The daily detail includes meal times: look at which days everything was "
                  "eaten inside the window, which days had food outside it, and whether that lines up with what was going on (late work, mood, weekends). "
                  "The window is just a tool to help them eat less; say plainly when it didn't hold, without making a big deal of it.\n" % wt)
    if about:
        about += "\n"
    prompt = (
        SAFETY_CLAUSE + p_["style"] + "\n\n"
        # These two are free text inside a %-formatted template -- one "5%" in them used to crash the whole review.
        + about.replace("%", "%%")
        + knowledge_text(period.get("member") or "default").replace("%", "%%") +
        "Below is everything the user logged from %s to %s. Write them a review of this stretch.\n\n"
        "Summary: %d days, %d with entries; %d weigh-ins; %d workouts, %d minutes; "
        "meals logged on %d days; %d journal entries. %s\n\n"
        "Day by day:\n%s\n\n"
        "%s"
        "This is a page for the user to read themselves -- not a work report, not a report card. "
        "Write like an old friend who knows what their days have been like, sitting down for a chat. "
        "A few emoji are fine (three to five in total), but no parallel-structure slogans, motivational lines or aphorisms.\n\n"
        "Use this structure, with ## headings exactly as given:\n"
        "## How these days went\n"
        "Say what you see, with specific dates and numbers. Skip empty praise like \"great job\" -- "
        "what lands is the specific thing: \"on the 9th you worked late, and the next morning you still stepped on the scale\".\n"
        "## A few things I noticed\n"
        "2–4 observations. Problems are fair game if there's evidence; the tone is sitting next to them, not standing over them.\n"
        "## Something to try tomorrow\n"
        "2–3 small things they could do tomorrow. No grand goals, no \"you should consider\" phrasing.\n"
        "## One more thing\n"
        "A sincere paragraph; it can run a bit longer. Tie it to something specific they did in this stretch, "
        "so it reads \"this is about me\", not \"this could be about anyone\". "
        "If they stopped and came back, say so -- that deserves more notice than an unbroken streak. "
        "End it plainly; don't wrap it up in a moral.\n\n"
        "Style:\n"
        "- vary sentence length\n"
        "- write \"you\", never \"the user\"\n"
        "- no stock phrases like \"keep up the good work\", \"proud of you\", \"journey\", \"crushing it\"\n"
        "- no em-dash pile-ups, no \"it's not X, it's Y\" closers\n\n"
        "Note: if the records are incomplete, say so -- don't fill gaps; weekly weight swings are common, "
        "so don't call a short-term blip a trend; never stoke body anxiety; avoid \"must\" and \"have to\"."
    ) % (
        period["start"], period["end"], period["total_days"], period["recorded_days"],
        period["weight_count"], period["exercise_count"], period["exercise_minutes"],
        period["meal_days"], period["diary_count"],
        ("Weight went from %s to %s (%+.1f lb)." % (_w(period["weight_first"]), _w(period["weight_last"]), period["weight_change"] * 2.20462))
        if period.get("weight_change") is not None else "Not enough weigh-ins to see a change.",
        detail,
        ("The user adds: %s\n\n" % str(note)[:300]) if note else "",
    )
    # 3000 isn't enough for reasoning models: thinking and the answer share the budget,
    # and a long think leaves nothing for the review after a 90-second wait.
    content = _call_llm_text(prompt, max_tokens=8000)
    if not (content or "").strip():
        content = _call_llm_text(
            prompt + "\n\nImportant: write the review directly -- no reasoning, no restating the instructions.",
            max_tokens=8000)
    if not (content or "").strip():
        llm_trace_dump("review_text", content)
        return {"ok": False, "err": "The review didn't come through. " + _llm_why()}
    return {"ok": True, "text": content.strip()}


def coach_week_report(persona, data):
    """A short weekly summary from structured week data."""
    key = ai_configured()
    if not key:
        return None
    p = PERSONAS.get(persona) or PERSONAS["warm"]
    style = SAFETY_CLAUSE + p["style"]
    weight_change = data.get("weight_change")
    wc_txt = ""
    if weight_change is not None:
        wc_txt = "Weight change this week: %+.1f lb (negative = down)" % (weight_change * 2.20462)
    prompt = (
        style + "\n\n"
        "Write the user's weekly health summary. This week:\n"
        "- " + (wc_txt or "Weight change: no weigh-ins") + "\n"
        "- Exercise: %s minutes\n"
        "- Average sleep: %s hours\n"
        "- Habit completion: %s%%\n"
        "- Days logged: %s\n\n"
        "Three short paragraphs (one or two sentences each): what went well, what to watch, one suggestion for next week. "
        "In your persona's voice; output only the text, no labels or markdown."
    ) % (data.get("exercise_min", 0), data.get("avg_sleep", "—"), data.get("checkin_rate", 0), data.get("record_days", 0))
    return _call_llm_text(prompt)


class Handler(BaseHTTPRequestHandler):
    # ---------- same-origin checks ----------
    # Listening on 127.0.0.1 keeps out other machines, not other websites in your own browser:
    # while the server runs, any page you visit could fetch it in the background. So instead of open CORS:
    #   1) Origin must be this server itself (or absent: a same-origin navigation)
    #   2) Host must be a loopback name (stops DNS rebinding pointing an outside domain at 127.0.0.1)
    def _host_name(self):
        raw = (self.headers.get("Host") or "")
        if raw.startswith("["):
            return raw.split("]")[0][1:]
        return raw.split(":")[0]

    def _allowed_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return ""          # same-origin request; browsers send no Origin
        allowed = set()
        for host in ("127.0.0.1", "localhost", "[::1]"):
            allowed.add("http://%s:%d" % (host, PORT))
        # Local addresses only. The server listens on no other interface, so there's no other legitimate origin.
        return origin if origin in allowed else None

    def _host_ok(self):
        h = self._host_name()
        # Loopback only. Against DNS rebinding: an outside domain resolving to 127.0.0.1 still fails,
        # because the Host header carries that domain name, which isn't on this list.
        return h in ("127.0.0.1", "localhost", "", "::1")

    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _guard(self):
        """Admission check. True to proceed; otherwise a 403 has been sent and the caller just returns.
        The server listens on 127.0.0.1 only, so anything not from this machine is refused, no exceptions.
        """
        ip = self.client_address[0] if self.client_address else ""
        if not is_loopback(ip):
            self._deny("Only this computer may connect")
            return False
        if not self._host_ok():
            self._deny("Host not allowed")
            return False
        if self._allowed_origin() is None:
            self._deny("Cross-site request refused")
            return False
        return True

    def _deny(self, msg):
        body = json.dumps({"err": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Read the request body, with a size cap. Raises ValueError over the limit."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            raise ValueError("Invalid Content-Length")
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request body too large")
        return self.rfile.read(length)

    def _cors(self):
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # no browser caching: an updated page should never be served stale
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not self._guard():
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._guard():
            return
        if self.path.split("?")[0] == "/api/data":
            self._json(200, load_data())
            return
        if self.path.split("?")[0] == "/api/photos":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            date = (q.get("date") or [""])[0]
            member = safe_member((q.get("member") or ["default"])[0])
            meta = load_photo_meta().get(member, {}).get(date, {})
            result = {}
            for meal in allowed_meals():
                url = None
                for ext in PHOTO_EXTS:
                    p = os.path.join(PHOTO_DIR, "%s_%s_%s.%s" % (member, date, meal, ext))
                    if os.path.exists(p):
                        url = "/photos/%s_%s_%s.%s" % (member, date, meal, ext)
                        break
                m = meta.get(meal) or {}
                # foods come back too: without them a logged meal couldn't be opened and edited,
                # only re-photographed -- and logging calories should never require a photo.
                result[meal] = {"url": url, "cal": m.get("cal"),
                                "items": m.get("items", ""),
                                "foods": m.get("foods") or [],
                                "at": clean_hm(m.get("at"))}
            self._json(200, result)
            return
        if self.path.split("?")[0] == "/api/meal_times":
            # When each meal was eaten in the last N days, and calories. For the eating-window card.
            # Times and calories only, no foods: small enough to fetch on every open.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            member = safe_member((q.get("member") or ["default"])[0])
            try:
                days = max(1, min(90, int((q.get("days") or ["14"])[0])))
            except Exception:
                days = 14
            today = datetime.date.today()
            meta = load_photo_meta().get(member, {})
            out = {}
            for back in range(days):
                d = (today - datetime.timedelta(days=back)).isoformat()
                day = meta.get(d) or {}
                got = {}
                for k, v in day.items():
                    if isinstance(v, dict) and v.get("cal") not in (None, ""):
                        got[k] = {"at": clean_hm(v.get("at")), "cal": v.get("cal")}
                if got:
                    out[d] = got
            self._json(200, {"ok": True, "today": today.isoformat(), "days": out})
            return
        if self.path.split("?")[0] == "/api/content":
            self._json(200, load_content())
            return
        if self.path.split("?")[0] == "/api/apikey":
            cfg = load_llm_config()
            k = cfg.get("api_key") or ""
            self._json(200, {
                "configured": bool(ai_configured()),
                "masked": ("****" + k[-4:]) if len(k) > 4 else ("****" if k else ""),
                "base_url": cfg.get("base_url") or "",
                "model": cfg.get("model") or "",
                "vision_model": cfg.get("vision_model") or "",
                "fast_model": cfg.get("fast_model") or "",
                "fast_model_effective": fast_model_of(cfg),
            })
            return
        if self.path.split("?")[0] == "/api/diary":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            member = safe_member((q.get("member") or ["default"])[0])
            date = (q.get("date") or [None])[0]
            diary = load_diary()
            md = diary.get(member, {})
            if date:
                self._json(200, {"date": date, "text": md.get(date, "")})
            else:
                # list every date with a journal entry for this member
                dates = sorted([d for d, t in md.items() if t.strip()], reverse=True)
                self._json(200, {"dates": dates, "count": len(dates)})
            return
        if self.path.split("?")[0] == "/api/knowledge":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            m = safe_member((q.get("member") or ["default"])[0])
            lst = load_knowledge().get(m) or []
            self._json(200, {"ok": True, "items": [
                {"id": x.get("id"), "title": x.get("title"),
                 "enabled": bool(x.get("enabled")), "len": len(x.get("text") or ""),
                 "preview": (x.get("text") or "")[:80]} for x in lst]})
            return
        if self.path.split("?")[0] == "/api/favorites":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            m = safe_member((q.get("member") or ["default"])[0])
            self._json(200, {"ok": True, "items": load_favs().get(m) or []})
            return
        if self.path.split("?")[0] == "/api/selfies":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(200, {"ok": True,
                             "items": selfie_list((q.get("member") or ["default"])[0])})
            return
        if self.path.split("?")[0] == "/api/attachments":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            member = safe_member((q.get("member") or ["default"])[0])
            category = (q.get("category") or [None])[0]
            attach = load_attach()
            items = attach.get(member, [])
            if category:
                items = [x for x in items if x.get("category") == category]
            items = sorted(items, key=lambda x: x.get("date", ""), reverse=True)
            self._json(200, {"items": items, "count": len(items)})
            return
        if self.path.split("?")[0] == "/api/version":
            self._json(200, {"ok": True, "build": BUILD, "pid": os.getpid()})
            return
        if self.path.split("?")[0] == "/api/export":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            member = safe_member((q.get("member") or ["default"])[0])
            d = load_data()
            m = get_member(d, member)
            if not m:
                self._json(404, {"err": "No such member"})
                return
            dates = set()
            for field in ["actual", "exercises", "sleep", "measurements", "naps"]:
                if isinstance(m.get(field), dict):
                    dates.update(m[field].keys())
            # days with only meals logged count too -- they used to vanish from the export
            for day, v in ((load_photo_meta() or {}).get(member) or {}).items():
                if isinstance(v, dict) and any(isinstance(x, dict) and x.get("cal") not in (None, "") for x in v.values()):
                    dates.add(day)
            dates = sorted([x for x in dates if isinstance(x, str)])
            out = io.StringIO()
            w = csv.writer(out)
            # Measurements were retired, but people who recorded them shouldn't lose them:
            # with history, the three columns are exported; without, new users get a clean header.
            has_meas = bool(m.get("measurements"))
            # Intake counts logged meals only (photos.json). The on-screen number also counts meals that were
            # only ticked off at their default calories; that lives in the browser and the server can't see it.
            # The column name says which number this is, so the two don't look like a mismatch.
            photos = (load_photo_meta() or {}).get(member) or {}
            prof = (load_content().get("profile") or {})
            weights = {k: v for k, v in (m.get("actual") or {}).items()
                       if isinstance(k, str) and isinstance(v, (int, float))}
            fallback_w = m.get("start_weight") or prof.get("start_weight") or 0

            def weight_on(day):
                """No weigh-in that day -> the most recent earlier one, then the plan's start weight."""
                if day in weights:
                    return weights[day]
                earlier = [k for k in weights if k <= day]
                if earlier:
                    return weights[max(earlier)]
                return fallback_w

            head = ["Date", "Weight lb", "Weight kg", "Exercise", "Exercise min", "Exercise kcal",
                    "Bedtime", "Wake time", "Sleep quality",
                    "Intake kcal (logged meals)", "Burn kcal (BMR+activity+exercise)", "Deficit kcal",
                    "First meal", "Last meal", "Eating span h",
                    "Protein g (meals with foods)", "Carbs g", "Fat g"]
            if has_meas:
                head += ["Waist cm", "Hip cm", "Body fat %"]
            w.writerow(head)
            for date in dates:
                actual = (m.get("actual") or {}).get(date)
                _exraw = (m.get("exercises") or {}).get(date) or {}
                _exarr = _exraw if isinstance(_exraw, list) else ([_exraw] if _exraw else [])
                # several workouts a day -> one row: types joined, minutes and calories summed
                ex = {"type": ", ".join(str(x.get("type") or "") for x in _exarr),
                      "duration": sum(int(x.get("duration") or 0) for x in _exarr) or "",
                      "kcal": sum(int(x.get("kcal") or 0) for x in _exarr) or ""} if _exarr else {}
                sl = (m.get("sleep") or {}).get(date) or {}
                ex_k = int(ex.get("kcal") or 0)
                meals = photos.get(date) or {}
                intake = 0
                for one in meals.values():
                    if isinstance(one, dict):
                        try:
                            intake += int(float(one.get("cal") or 0))
                        except Exception:
                            pass
                burn = _daily_burn(weight_on(date), prof, ex_k)
                # With no meals logged, intake would be 0 and the deficit the whole day's burn --
                # that's "not logged", not "didn't eat". Those days leave the three columns empty.
                gap = (burn - intake) if (meals and burn) else ""
                row = [date,
                       round(actual * 2.20462, 1) if actual is not None else "",
                       actual if actual is not None else "",
                       ex.get("type", ""), ex.get("duration", ""), ex.get("kcal", ""),
                       sl.get("bed", ""), sl.get("wake", ""), sl.get("quality", ""),
                       intake if meals else "", burn or "", gap]
                ts = [x[0] for x in _meal_times(meals)]
                if ts:
                    span = (_hm_to_min(ts[-1]) - _hm_to_min(ts[0])) / 60.0
                    row += [ts[0], ts[-1], ("%.1f" % span) if len(ts) > 1 else ""]
                else:
                    row += ["", "", ""]
                mac, n_known, n_all = day_macros(meals)
                row += [mac["p"], mac["c"], mac["f"]] if n_known else ["", "", ""]
                if has_meas:
                    me = (m.get("measurements") or {}).get(date) or {}
                    row += [me.get("waist", ""), me.get("hip", ""), me.get("fat", "")]
                w.writerow(row)
            body = out.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="bodycraft_export.csv"')
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # static files
        rel = urllib.parse.unquote(self.path.split("?")[0].lstrip("/"))
        if rel == "":
            rel = "bodycraft.html"
        fp = os.path.realpath(os.path.join(BASE, rel))
        if not fp.startswith(os.path.realpath(BASE) + os.sep) or not os.path.isfile(fp):
            self._json(404, {"err": "not found"})
            return
        ext = os.path.splitext(fp)[1].lower().lstrip(".")
        ctype = {"html": "text/html", "js": "application/javascript",
                 "css": "text/css", "json": "application/json",
                 "svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg",
                 "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
                 "ico": "image/x-icon", "pdf": "application/pdf",
                 "doc": "application/msword", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "txt": "text/plain", "md": "text/plain",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}.get(ext, "application/octet-stream")
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._guard():
            return
        if self.path.split("?")[0] == "/api/knowledge":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                member = safe_member(d.get("member") or "default")
                store = load_knowledge()
                items = store.setdefault(member, [])
                act = d.get("action")
                if act == "add":
                    text = str(d.get("text") or "").strip()
                    if not text:
                        self._json(400, {"ok": False, "err": "It's empty"})
                        return
                    if len(text) > 20000:
                        text = text[:20000]
                    items.append({"id": "k%d" % int(datetime.datetime.now().timestamp() * 1000),
                                  "title": str(d.get("title") or "My method")[:40],
                                  "text": text, "enabled": True})
                elif act == "toggle":
                    for x in items:
                        if x.get("id") == d.get("id"):
                            x["enabled"] = not x.get("enabled")
                elif act == "delete":
                    store[member] = [x for x in items if x.get("id") != d.get("id")]
                elif act == "rename":
                    for x in items:
                        if x.get("id") == d.get("id"):
                            x["title"] = str(d.get("title") or "")[:40] or x["title"]
                save_knowledge(store)
                lst = store.get(member) or []
                self._json(200, {"ok": True, "items": [
                    {"id": x.get("id"), "title": x.get("title"),
                     "enabled": bool(x.get("enabled")), "len": len(x.get("text") or ""),
                     "preview": (x.get("text") or "")[:80]} for x in lst]})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/review":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                member = safe_member(d.get("member") or "default")
                start = str(d.get("start") or "")[:10]
                end = str(d.get("end") or "")[:10]
                if len(start) != 10 or len(end) != 10:
                    self._json(400, {"ok": False, "err": "Pick a start and end date"})
                    return
                if start > end:
                    start, end = end, start
                span = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days
                if span > 400:
                    self._json(400, {"ok": False, "err": "That range is too long -- one year at most"})
                    return
                period = collect_period(member, start, end,
                                        include_diary=bool(d.get("include_diary", True)))
                period["member"] = member
                if d.get("preview"):
                    self._json(200, {"ok": True, "period": period,
                                     "badges": period_badges(period)})
                    return
                r = review_text(period, d.get("persona") or "warm", d.get("note") or "")
                if not r.get("ok"):
                    self._json(200, r)
                    return
                self._json(200, {"ok": True, "text": r["text"],
                                 "period": period,
                                 "badges": period_badges(period)})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/selfie":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                member = safe_member(d.get("member") or "default")
                date = str(d.get("date") or "")[:10]
                if len(date) != 10:
                    self._json(400, {"ok": False, "err": "Invalid date format"})
                    return
                meta = load_selfies()
                day = (meta.setdefault(member, {})).setdefault(date, [])

                if d.get("delete"):
                    # Delete one. The file name must be in our own generated format; no arbitrary paths
                    fn = os.path.basename(str(d.get("file") or ""))
                    day[:] = [x for x in day if x.get("file") != fn]
                    if not day:
                        meta[member].pop(date, None)
                    save_selfies(meta)
                    try:
                        os.makedirs(os.path.join(SELFIE_DIR, ".trash"), exist_ok=True)
                        src = os.path.join(SELFIE_DIR, fn)
                        if os.path.exists(src):
                            os.replace(src, os.path.join(SELFIE_DIR, ".trash", fn))
                    except Exception:
                        pass
                    self._json(200, {"ok": True})
                    return

                if d.get("note_only"):
                    fn = os.path.basename(str(d.get("file") or ""))
                    for x in day:
                        if x.get("file") == fn:
                            x["note"] = str(d.get("note") or "")[:200]
                    save_selfies(meta)
                    self._json(200, {"ok": True})
                    return

                ext = str(d.get("ext") or "jpg").lower().lstrip(".")
                if ext not in PHOTO_EXTS:
                    self._json(400, {"ok": False, "err": "Image format not supported"})
                    return
                raw = base64.b64decode(str(d.get("data") or "").split(",")[-1])
                if len(raw) > 12 * 1024 * 1024:
                    self._json(400, {"ok": False, "err": "That image is too large"})
                    return
                os.makedirs(SELFIE_DIR, exist_ok=True)
                # several per day are allowed, numbered
                idx = 1
                while True:
                    fname = "%s_%s_%d.%s" % (member, date, idx, ext)
                    if not os.path.exists(os.path.join(SELFIE_DIR, fname)):
                        break
                    idx += 1
                with open(os.path.join(SELFIE_DIR, fname), "wb") as f:
                    f.write(raw)
                day.append({"file": fname, "note": str(d.get("note") or "")[:200],
                            "weight": d.get("weight")})
                save_selfies(meta)
                self._json(200, {"ok": True, "url": "/selfies/" + fname, "file": fname})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/photo":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                date = (d.get("date") or "").strip()
                meal = d.get("meal") or ""
                member = safe_member(d.get("member"))
                if d.get("delete"):
                    if meal not in allowed_meals() or len(date) != 10 \
                            or any(c not in "0123456789-" for c in date):
                        self._json(400, {"ok": False, "err": "Invalid parameters"})
                        return
                    delete_photo(date, meal, member)
                    self._json(200, {"ok": True})
                    return
                data = d.get("data") or ""
                ext = (d.get("ext") or "jpg").lower().lstrip(".")
                if meal not in allowed_meals() or ext not in PHOTO_EXTS \
                        or not data or len(date) != 10 \
                        or any(c not in "0123456789-" for c in date):
                    self._json(400, {"ok": False, "err": "Invalid parameters"})
                    return
                binary = base64.b64decode(data)
                os.makedirs(PHOTO_DIR, exist_ok=True)
                delete_photo(date, meal, member)
                fname = "%s_%s_%s.%s" % (member, date, meal, ext)
                photo_path = os.path.join(PHOTO_DIR, fname)
                with open(photo_path, "wb") as f:
                    f.write(binary)
                if isinstance(d.get("foods"), list) and d.get("foods"):
                    # An image the coach already read: save the photo to this meal and keep its food list -- don't ask the vision model again
                    foods = []
                    for f in d["foods"][:40]:
                        if not isinstance(f, dict):
                            continue
                        nm = str(f.get("name") or "").strip()[:40]
                        if not nm:
                            continue
                        try:
                            wt = _amount(f.get("weight"))
                            cal = max(0, min(5000, int(float(f.get("cal") or 0))))
                        except Exception:
                            wt, cal = 0, 0
                        foods.append(dict({"name": nm, "weight": wt, "cal": cal}, **macro_fields(f)))
                    total = sum(x["cal"] for x in foods)
                    items = ", ".join(x["name"] for x in foods)
                else:
                    foods, total, items = recognize_meal(photo_path)
                self._json(200, {"ok": True, "url": "/photos/" + fname,
                                 "foods": foods, "total": total, "items": items})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/photo_meta":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                date = (d.get("date") or "").strip()
                meal = d.get("meal") or ""
                member = safe_member(d.get("member"))
                if meal not in allowed_meals() or len(date) != 10 \
                        or any(c not in "0123456789-" for c in date):
                    self._json(400, {"ok": False, "err": "Invalid parameters"})
                    return
                foods = d.get("foods") or []
                if not isinstance(foods, list):
                    foods = []
                # keep only these fields per food; macros only if all three are present
                clean_foods = []
                for f in foods[:40]:
                    if not isinstance(f, dict):
                        continue
                    item = {"name": str(f.get("name") or "")[:40]}
                    item["weight"] = _amount(f.get("weight"))
                    try:
                        item["cal"] = max(0, int(float(f.get("cal") or 0)))
                    except Exception:
                        item["cal"] = 0
                    if f.get("unit"):
                        item["unit"] = str(f.get("unit"))[:8]
                    item.update(macro_fields(f))
                    clean_foods.append(item)
                foods = clean_foods
                meta = load_photo_meta()
                rec = {"cal": d.get("cal"), "items": d.get("items") or "", "foods": foods}
                # When it was eaten: the eating window and fasting time depend on it.
                # Not sent -> keep the meal's previous time; editing calories must not erase it.
                at = clean_hm(d.get("at"))
                old = ((meta.get(member) or {}).get(date) or {}).get(meal) or {}
                if at:
                    rec["at"] = at
                elif "at" not in d and isinstance(old, dict) and old.get("at") \
                        and rec["cal"] not in (None, ""):
                    rec["at"] = old["at"]
                meta.setdefault(member, {}).setdefault(date, {})[meal] = rec
                save_photo_meta(meta)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/content":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                if not isinstance(d, dict):
                    self._json(400, {"ok": False, "err": "Invalid content format"})
                    return
                # Merge by top-level field rather than replacing everything:
                # the first-run setup writes only profile/weeks/schedule, and a full replace would quietly wipe
                # the menu, habits and rules already there.
                # To replace on purpose, send {"replace": true, "content": {...}}.
                with _lock:
                    if d.get("replace") is True and isinstance(d.get("content"), dict):
                        save_content(d["content"])
                    else:
                        cur = load_content()
                        for k, v in d.items():
                            if k == "profile" and isinstance(v, dict):
                                prof = cur.get("profile")
                                cur["profile"] = dict(prof if isinstance(prof, dict) else {})
                                cur["profile"].update(sanitize_profile(v))
                            else:
                                cur[k] = v
                        save_content(cur)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/data":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                if not isinstance(d, dict):
                    self._json(400, {"ok": False, "err": "Invalid data format"})
                    return
                # /api/data replaces the whole file. With two tabs open, the later save would silently
                # wipe everything the other one saved. A revision number guards against it: the client sends
                # the rev it read; if it doesn't match, refuse and send back the latest data.
                with _lock:
                    cur = load_data()
                    cur_rev = int(cur.get("rev") or 0)
                    cli_rev = d.get("rev")
                    if cli_rev is not None and int(cli_rev) != cur_rev:
                        self._json(409, {"ok": False, "err": "Your data was changed in another window",
                                         "rev": cur_rev, "data": cur})
                        return
                    save_data(d)
                    self._json(200, {"ok": True, "rev": int(d.get("rev") or 0)})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/member":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                data = load_data()
                action = d.get("action")
                if action == "add":
                    name = (d.get("name") or "").strip() or ("Member %d" % (len(data["members"]) + 1))
                    mid = "m%d" % int(datetime.datetime.now().timestamp() * 1000)
                    data["members"].append({"id": mid, "name": name,
                                            "start_weight": 78.0, "targets": [], "actual": {}})
                    data["active"] = mid
                elif action == "remove":
                    mid = d.get("id")
                    if mid == "default":
                        self._json(400, {"ok": False, "err": "The main profile can't be deleted"})
                        return
                    data["members"] = [m for m in data["members"] if m.get("id") != mid]
                    if data.get("active") == mid:
                        data["active"] = data["members"][0]["id"]
                elif action == "rename":
                    mid = d.get("id")
                    name = (d.get("name") or "").strip()
                    for m in data["members"]:
                        if m.get("id") == mid:
                            m["name"] = name or m["name"]
                save_data(data)
                self._json(200, {"ok": True, "data": data})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/active":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                data = load_data()
                mid = d.get("id")
                if get_member(data, mid):
                    data["active"] = mid
                    save_data(data)
                    self._json(200, {"ok": True})
                else:
                    self._json(400, {"ok": False, "err": "No such member"})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/diary":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                member = safe_member(d.get("member"))
                date = (d.get("date") or "").strip()
                text = d.get("text") or ""
                if len(date) != 10 or any(c not in "0123456789-" for c in date):
                    self._json(400, {"ok": False, "err": "Invalid date format"})
                    return
                if d.get("delete"):
                    diary = load_diary()
                    md = diary.get(member, {})
                    md.pop(date, None)
                    if not md: diary.pop(member, None)
                    save_diary(diary)
                else:
                    diary = load_diary()
                    md = diary.setdefault(member, {})
                    if text.strip():
                        md[date] = text
                    else:
                        md.pop(date, None)
                        if not md: diary.pop(member, None)
                    save_diary(diary)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/member_setup":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                arr = []
                try:
                    with open(SETUP_FILE, "r", encoding="utf-8") as f:
                        arr = json.load(f)
                    if not isinstance(arr, list): arr = []
                except Exception:
                    pass
                mid = d.get("member_id")
                arr = [x for x in arr if x.get("member_id") != mid]
                arr.append(d)
                with open(SETUP_FILE, "w", encoding="utf-8") as f:
                    json.dump(arr, f, ensure_ascii=False, indent=2)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/apikey":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                if d.get("clear"):
                    clear_llm_config()
                    self._json(200, {"ok": True, "configured": False})
                else:
                    key = (d.get("api_key") or d.get("key") or "").strip()
                    if not key:
                        self._json(400, {"ok": False, "err": "The key is empty"})
                        return
                    save_llm_config(d)
                    self._json(200, {"ok": True, "configured": True,
                                     "masked": ("****" + key[-4:]) if len(key) > 4 else "****"})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/llm_update":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                text = (d.get("text") or "").strip()
                if not text:
                    self._json(400, {"ok": False, "err": "The request is empty"})
                    return
                if len(text) > 2000:
                    self._json(400, {"ok": False, "err": "The request is too long"})
                    return
                result = llm_update_content(text, d.get("base"))
                self._json(200 if result.get("ok") else 500, result)
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/wipe":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                if d.get("scope") != "records":
                    self._json(400, {"ok": False, "err": "Unknown reset option"})
                    return
                self._json(200, wipe_records())
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/food_lookup_batch":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                names = d.get("names")
                if isinstance(names, str):
                    names = re.split(r"\s*[,;\n]+\s*", names)
                self._json(200, food_lookup_batch(names or [], safe_member(d.get("member") or "default")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/food_image":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                self._json(200, recognize_food_image(d.get("data"), d.get("ext")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/fetch_recipe":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                url = str(d.get("url") or "").strip()
                if not url:
                    self._json(400, {"ok": False, "err": "Paste a link"})
                    return
                if len(url) > 500:
                    self._json(400, {"ok": False, "err": "That link is too long"})
                    return
                self._json(200, fetch_recipe(url, safe_member(d.get("member") or "default")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/favorites":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                member = safe_member(d.get("member") or "default")
                act = d.get("action")
                if act == "add":
                    self._json(200, fav_add(member, d.get("item")))
                    return
                store = load_favs()
                lst = store.setdefault(member, [])
                if act == "delete":
                    store[member] = [x for x in lst if x.get("id") != d.get("id")]
                    save_favs(store)
                elif act == "note":
                    for x in lst:
                        if x.get("id") == d.get("id"):
                            x["desc"] = str(d.get("desc") or "")[:200]
                    save_favs(store)
                self._json(200, {"ok": True, "items": store.get(member) or []})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/food_lookup":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                if d.get("action") == "list":
                    tb = load_lookup()
                    self._json(200, {"ok": True, "items": [
                        dict({"key": k, "name": v.get("name"), "unit": v.get("unit"),
                              "per": v.get("per"), "note": v.get("note", ""),
                              "category": food_category(v.get("name"), v.get("category"))},
                             **macro_raw(v))
                        for k, v in sorted(tb.items())]})
                    return
                if d.get("action") == "delete":
                    tb = load_lookup()
                    tb.pop(str(d.get("key") or ""), None)
                    save_lookup(tb)
                    self._json(200, {"ok": True})
                    return
                if d.get("action") == "save":
                    tb = load_lookup()
                    nm = str(d.get("name") or "").strip()[:40]
                    un = str(d.get("unit") or "g").strip()[:8]
                    try:
                        per = float(d.get("per"))
                    except Exception:
                        per = 0
                    if nm and 0 < per <= 9000:
                        old = tb.get(lookup_key(nm, un)) or {}
                        # Editing calories by hand without macros: keep the old ones (changing calories shouldn't erase protein)
                        tb[lookup_key(nm, un)] = dict({"per": per, "name": nm, "unit": un,
                                                       "note": str(d.get("note") or "")[:40],
                                                       "category": food_category(nm, d.get("category"))},
                                                      **(macro_raw(d) or macro_raw(old)))
                        save_lookup(tb)
                        self._json(200, {"ok": True})
                    else:
                        self._json(400, {"ok": False, "err": "Check the name and number"})
                    return
                self._json(200, food_lookup(d.get("name"), d.get("unit"), d.get("amount")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/coach_line":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                self._json(200, coach_line(d.get("stats") or {}))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/meal_ideas":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                self._json(200, meal_ideas(d.get("slot"), d.get("question") or "",
                                           d.get("context"), d.get("slot_name") or ""))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/coach_image":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                self._json(200, coach_image(d.get("data"), d.get("ext"), d.get("text"), d.get("meal_names")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/food_advice":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                q = (d.get("question") or "").strip()
                img = d.get("image") or None
                if not q and not img:
                    self._json(400, {"ok": False, "err": "Ask something, or add a photo"})
                    return
                if len(q) > 800:
                    self._json(400, {"ok": False, "err": "That question is too long"})
                    return
                self._json(200, food_advice(q or "Here's what I have -- any meal ideas?",
                                            img, d.get("context")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/intent":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                text = (d.get("text") or "").strip()
                intent = classify_intent(text, d.get("context") or "")
                # distress: hear them out first; recordable: there's something loggable in the message,
                # so the page offers a "log it?" button after replying -- the user decides.
                self._json(200, {"ok": True, "intent": intent,
                                 "distress": distress_kind(text),
                                 "recordable": bool(record_kind(text))})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/kcal_foods":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                self._json(200, kcal_for_foods(d.get("foods")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/kcal_exercise":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                self._json(200, exercise_kcal(d.get("type"), d.get("duration"),
                                              d.get("intensity"), d.get("weight")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/meal_text":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                text = (d.get("text") or "").strip()
                if not text:
                    self._json(400, {"ok": False, "err": "Say something first"})
                    return
                if len(text) > 1500:
                    self._json(400, {"ok": False, "err": "That's too long"})
                    return
                keys = [k for k in (d.get("meals") or []) if isinstance(k, str)] or allowed_meals()
                self._json(200, meals_from_text(text, keys))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/daily_log":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                text = (d.get("text") or "").strip()
                if not text:
                    self._json(400, {"ok": False, "err": "Tell me something about your day"})
                    return
                if len(text) > 2000:
                    self._json(400, {"ok": False, "err": "That's a lot -- splitting it into two messages will be more accurate"})
                    return
                self._json(200, daily_log_from_text(
                    text, d.get("meals"), d.get("habits"), d.get("exercise_types")))
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/check_goal":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                level, msg, suggest = check_goal(d.get("start_weight"), d.get("target"),
                                                 d.get("weeks"), d.get("height_cm"))
                self._json(200, {"ok": True, "level": level, "msg": msg,
                                 "suggest_weeks": suggest})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/rollback":
            try:
                ok = rollback_last()
                self._json(200, {"ok": ok, "err": None if ok else "Nothing to undo"})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/coach":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                persona = d.get("persona") or "warm"
                if persona not in PERSONAS:
                    persona = "warm"
                events = d.get("events") or []
                context = d.get("context") or {}
                history = d.get("history") or None
                reply = coach_reply(persona, events, context, history)
                if reply:
                    self._json(200, {"ok": True, "reply": reply, "name": PERSONAS[persona]["name"]})
                elif not ai_configured():
                    # Keep "no key" and "real error" apart. Both used to be a 500 with "the coach can't
                    # reply right now", and users couldn't tell whether to add a key or just wait.
                    self._json(200, {"ok": False, "err": "AI isn't set up yet", "need_setup": True})
                else:
                    self._json(200, {"ok": False, "err": "The coach didn't manage an answer this time -- try again"})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/week_report":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                persona = d.get("persona") or "warm"
                if persona not in PERSONAS:
                    persona = "warm"
                reply = coach_week_report(persona, d.get("data") or {})
                if reply:
                    self._json(200, {"ok": True, "reply": reply})
                else:
                    self._json(500, {"ok": False, "err": "The coach can't reply right now"})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        if self.path.split("?")[0] == "/api/attach":
            try:
                d = json.loads(self._read_body().decode("utf-8"))
                member = safe_member(d.get("member"))
                # delete
                if d.get("delete"):
                    aid = d.get("id") or ""
                    attach = load_attach()
                    items = attach.get(member, [])
                    target = next((x for x in items if x.get("id") == aid), None)
                    items = [x for x in items if x.get("id") != aid]
                    attach[member] = items
                    save_attach(attach)
                    if target:
                        p = os.path.join(ATTACH_DIR, os.path.basename(target.get("path") or ""))
                        try:
                            os.replace(p, os.path.join(ATTACH_DIR, ".trash", os.path.basename(p)))
                        except Exception:
                            pass
                    self._json(200, {"ok": True})
                    return
                # upload
                name = (d.get("name") or "file").strip()[:120]
                category = d.get("category") or "library"
                date = (d.get("date") or "").strip()
                note = (d.get("note") or "").strip()[:200]
                data = d.get("data") or ""
                ext = (d.get("ext") or "bin").lower().lstrip(".")
                if ext not in ("jpg", "jpeg", "png", "webp", "gif", "pdf", "doc", "docx", "txt", "md", "xlsx", "pptx", "csv", "zip", "bin"):
                    ext = "bin"
                if not data:
                    self._json(400, {"ok": False, "err": "The file is empty"})
                    return
                binary = base64.b64decode(data)
                if len(binary) > 30 * 1024 * 1024:
                    self._json(400, {"ok": False, "err": "File too large (over 30 MB)"})
                    return
                os.makedirs(ATTACH_DIR, exist_ok=True)
                aid = "a%d" % int(datetime.datetime.now().timestamp() * 1000)
                fname = "%s_%s.%s" % (aid, os.path.splitext(name)[0][:40] or "file", ext)
                fname = re.sub(r'[\\/:*?"<>|]', "_", fname)
                path = os.path.join(ATTACH_DIR, fname)
                with open(path, "wb") as f:
                    f.write(binary)
                item = {"id": aid, "name": name, "path": "files/" + fname,
                        "type": "image" if ext in ("jpg", "jpeg", "png", "webp", "gif") else "file",
                        "ext": ext, "category": category, "date": date or datetime.date.today().isoformat(),
                        "note": note, "size": len(binary)}
                attach = load_attach()
                attach.setdefault(member, []).append(item)
                save_attach(attach)
                self._json(200, {"ok": True, "item": item})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return
        self._json(404, {"err": "not found"})

    def log_message(self, *args):
        pass


def _stop_previous_instance():
    """Stop only the process *this app* started last time -- never kill by port.

    An earlier version killed whatever held the port, without checking whose process it was: any program
    that happened to use the same port would be force-quit at startup. Now only the PID this app wrote
    into .server_running.json is trusted.
    """
    try:
        with open(RUN_FLAG, "r", encoding="utf-8") as f:
            info = json.load(f)
        pid = int(info.get("pid") or 0)
    except Exception:
        return
    if not pid or pid == os.getpid():
        return
    try:
        import subprocess as _sp
        if os.name == "nt":
            # double-check the image name: only stop it if it really is a python process
            out = _sp.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                          capture_output=True).stdout.decode("gbk", "ignore").lower()
            if "python" not in out:
                return
            # without /F, windowless pythonw usually ignores it: polite first, then forced.
            # The safety boundary is the identity check above (our own PID + image name), not avoiding /F.
            _sp.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
            import time as _t
            _t.sleep(1.0)
            still = _sp.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                            capture_output=True).stdout.decode("gbk", "ignore").lower()
            if "python" in still:
                _sp.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
        else:
            import signal, time as _t
            os.kill(pid, signal.SIGTERM)
            # wait for it to let go of the port, so the new server can take the same one
            # (browser-side check-ins are stored per address; a new port would hide them)
            for _ in range(30):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                _t.sleep(0.1)
        return int(info.get("port") or 0) or None
    except Exception:
        pass


def _amount(v):
    """A food amount: grams or a count of cups/slices. Fractions are real here (0.75 cup), so keep
    two decimals instead of truncating to a whole number; whole numbers stay ints."""
    try:
        x = round(max(0.0, min(5000.0, float(v or 0))), 2)
    except Exception:
        return 0
    return int(x) if x == int(x) else x


def _bind(port):
    # Loopback only. There is deliberately no switch to listen on other interfaces --
    # one fewer switch means one fewer way to open it by mistake.
    host = "127.0.0.1"
    try:
        # allow_reuse_address: after a crash, TIME_WAIT can make the port look busy when nothing
        # is using it. This lets us take it over immediately instead of waiting a minute or two
        # after a reboot or forced quit.
        ThreadingHTTPServer.allow_reuse_address = True
        return ThreadingHTTPServer((host, port), Handler)
    except OSError:
        return None


def main():
    global PORT
    old_port = _stop_previous_instance()

    srv = None
    if old_port:
        # the old instance just quit; its socket can take a moment to free up
        import time as _t
        for _ in range(15):
            srv = _bind(old_port)
            if srv:
                PORT = old_port
                break
            _t.sleep(0.2)
    first = PORT
    for _try in range(0 if srv else 12):          # port taken by someone else -> move to the next one, don't fight
        srv = _bind(PORT)
        if srv:
            break
        PORT += 1
    if not srv:
        print("Ports %d-%d are all in use; couldn't start" % (first, first + 11))
        return

    try:
        # write the port to the run flag so start.py and the widget can find it
        with open(RUN_FLAG, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": PORT}, f)
    except Exception:
        pass

    seed_lookup_if_empty()
    backfill_seed_macros()
    print("Bodycraft · local server running at http://127.0.0.1:%d  version %s  (by @mmjj324)" % (PORT, BUILD))
    try:
        srv.serve_forever()
    finally:
        try:
            os.remove(RUN_FLAG)
        except Exception:
            pass


if __name__ == "__main__":
    main()
