# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""Unit tests for the core pure functions. Run: python3 tests/test_unit.py

Covers the most fragile functions on the AI-update pipeline, plus the safety guards.
When these go wrong they don't raise -- they quietly produce wrong content.
If _merge_list_items mismatched, the user would just see two days missing from
their menu, not an exception. That's why they're worth watching with tests.

The server is copied into a temp folder before import, so nothing it writes
(debug traces, lookups) lands in the project folder, and a stray content.json
there can't change the unit preference these tests rely on.
"""
import os
import sys
import json
import shutil
import datetime
import tempfile
import unittest
import importlib.util
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)

_TMP = tempfile.mkdtemp(prefix="bodycraft_unit_")
shutil.copy(os.path.join(BASE, "bodycraft_server.py"), os.path.join(_TMP, "svc.py"))
_spec = importlib.util.spec_from_file_location("svc", os.path.join(_TMP, "svc.py"))
svc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svc)

LB = 2.20462


def lb(x):
    """Pounds -> kilograms, the way the server rounds it."""
    return round(x / LB, 2)


class TestMemberSanitizing(unittest.TestCase):
    """member is put into file names: the one place outside input goes straight into a path."""

    def test_valid_values_pass_through(self):
        self.assertEqual(svc.safe_member("default"), "default")
        self.assertEqual(svc.safe_member("m1735689600000"), "m1735689600000")

    def test_path_traversal_blocked(self):
        for bad in ["../../../etc/passwd", "..\\..\\windows", "../..",
                    "m1/../../x", "a/b", "a\\b"]:
            self.assertEqual(svc.safe_member(bad), "default", bad)

    def test_empty_and_odd_input(self):
        for bad in [None, "", "   ", 123, {"a": 1}, "m" + "9" * 50]:
            self.assertEqual(svc.safe_member(bad), "default", repr(bad))


class TestGoalGuard(unittest.TestCase):
    """check_goal works in kilograms internally; messages talk to the user in pounds."""

    def test_reasonable_goal_passes(self):
        level, _, _ = svc.check_goal(80, 72, 16, 175)
        self.assertEqual(level, "ok")

    def test_too_fast_is_slowed_down_not_refused(self):
        level, msg, weeks = svc.check_goal(100, 70, 18, None)
        self.assertEqual(level, "warn")
        self.assertEqual(weeks, 30)          # 30 kg / 1.0 kg per week
        self.assertIn("steady", msg)

    def test_warning_speaks_pounds(self):
        _, msg, _ = svc.check_goal(100, 70, 18, None)
        self.assertIn("66 lb", msg)          # 30 kg is about 66 lb
        self.assertNotIn("30 kg", msg)

    def test_below_height_floor_is_refused(self):
        level, msg, _ = svc.check_goal(75, 45, 40, 175)
        self.assertEqual(level, "block")
        self.assertIn("doctor", msg)
        self.assertIn("lb", msg)

    def test_without_height_falls_back_to_absolute_range(self):
        self.assertEqual(svc.check_goal(75, 45, 40, None)[0], "ok")
        self.assertEqual(svc.check_goal(75, 20, 40, None)[0], "block")

    def test_gaining_or_maintaining_is_outside_the_guard(self):
        self.assertEqual(svc.check_goal(60, 65, 8, 170)[0], "ok")
        self.assertEqual(svc.check_goal(60, 60, 8, 170)[0], "ok")

    def test_bad_input_is_refused_not_raised(self):
        for a, b, c in [("x", 70, 10), (80, None, 10), (80, 70, "abc")]:
            self.assertIn(svc.check_goal(a, b, c)[0], ("ok", "block"))


class TestIncrementalMerge(unittest.TestCase):
    """The model often returns only the items it changed; overwriting wholesale would make the rest vanish."""

    def test_existing_item_updated_by_key(self):
        cur = [{"day": "Mon", "base": "Beef stew", "tip": "saves time"},
               {"day": "Tue", "base": "Chicken breast"}]
        new = [{"day": "Mon", "base": "Beef and tomato stew"}]
        out = svc._merge_list_items(cur, new, "day")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["base"], "Beef and tomato stew")
        self.assertEqual(out[0]["tip"], "saves time")          # fields not mentioned are kept
        self.assertEqual(out[1]["base"], "Chicken breast")     # days not mentioned are kept

    def test_new_item_appended(self):
        out = svc._merge_list_items([{"day": "Mon"}], [{"day": "Wed"}], "day")
        self.assertEqual([x["day"] for x in out], ["Mon", "Wed"])

    def test_empty_value_does_not_overwrite(self):
        cur = [{"day": "Mon", "base": "Beef stew"}]
        out = svc._merge_list_items(cur, [{"day": "Mon", "base": ""}], "day")
        self.assertEqual(out[0]["base"], "Beef stew")

    def test_fuzzy_match_for_routine_titles(self):
        cur = [{"title": "Morning workout · brisk walk 40 min", "t": "07:50"}]
        new = [{"title": "Morning workout", "t": "08:00"}]
        out = svc._merge_list_items(cur, new, "title", fuzzy=True)
        self.assertEqual(len(out), 1)          # recognized as the same item, not a new one
        self.assertEqual(out[0]["t"], "08:00")

    def test_empty_input_does_not_crash(self):
        self.assertEqual(svc._merge_list_items([], [], "day"), [])
        self.assertEqual(len(svc._merge_list_items(None, [{"day": "Mon"}], "day")), 1)


class TestGoalWeightExtraction(unittest.TestCase):
    """_extract_target_change returns kilograms. Pounds are converted; no unit means the user's preference (lb by default)."""

    def test_common_phrasings(self):
        for text, want in [
            ("change my goal weight to 150 lb", lb(150)),
            ("set goal weight to 68 kg", 68.0),
            ("I want to get down to 145 pounds", lb(145)),
            ("my goal is 140 lbs", lb(140)),
            ("drop to 62 kg", 62.0),
            ("change my weight from 180 lb to 155 lb", lb(155)),
        ]:
            got = svc._extract_target_change(text)
            self.assertEqual(got, want, "%s -> %s" % (text, got))

    def test_no_unit_uses_pound_preference(self):
        """Default users think in pounds: "goal weight 150" is 150 lb, not 150 kg."""
        self.assertEqual(svc._extract_target_change("target weight 150"), lb(150))

    def test_no_unit_uses_kg_preference_when_set(self):
        with mock.patch.object(svc, "load_content", return_value={"profile": {"weight_unit": "kg"}}):
            self.assertEqual(svc._extract_target_change("target weight 70"), 70.0)
            # an explicit unit always wins over the preference
            self.assertEqual(svc._extract_target_change("goal weight 154 lb"), lb(154))

    def test_other_goals_are_not_the_goal_weight(self):
        """"Change my nap goal to 40 minutes" once matched as "goal ... 40" -> goal weight 40.
        Users with a height set ran straight into the low-weight guard; users without one were
        worse off -- their whole plan was silently rebuilt and they had no idea why."""
        for text in ["change my nap goal to 40 minutes", "set my nap target to 40 min",
                     "change my water goal to 3000", "set workout goal to 90 minutes",
                     "change daily calorie target to 1600", "sleep goal is 8 hours",
                     "set my step goal to 10000", "change hydration target to 80 oz",
                     "change exercise time from 30 minutes to 60 minutes",
                     "protein target to 120"]:
            self.assertIsNone(svc._extract_target_change(text), text)

    def test_numbers_outside_human_weights_never_count(self):
        self.assertIsNone(svc._extract_target_change("change my goal weight to 3000"))
        self.assertIsNone(svc._extract_target_change("goal weight 5 kg"))
        self.assertIsNone(svc._extract_target_change("goal weight 40 lb"))   # 18 kg

    def test_unrelated_text_not_misread(self):
        # "Get lunch down to 300 calories" is not a body weight
        for text in ["swap dinner for fish", "add a swim on Wednesday", "",
                     "get lunch down to 300 calories", "bring workouts down to 30 minutes",
                     "lost 3 pounds this week"]:
            self.assertIsNone(svc._extract_target_change(text), text)


class TestNaturalLanguageLogging(unittest.TestCase):

    def test_workout_log_does_not_fall_into_chat_or_update(self):
        self.assertEqual(svc.classify_intent("log a workout: walked 30 minutes, 3000 steps"), "record")
        self.assertEqual(svc.classify_intent("add that to my workouts", "walked 30 minutes, 3000 steps"), "record")

    def test_mixed_log_uses_the_single_entry_point(self):
        text = ("had oatmeal and coffee for breakfast, walked 30 min, "
                "went to bed at 11pm, 182.4 lb this morning")
        self.assertEqual(svc.classify_intent(text), "record")

    def test_changing_the_exercise_goal_is_still_a_settings_update(self):
        self.assertEqual(svc.classify_intent("change my exercise goal to 60 minutes"), "update")
        self.assertEqual(svc.classify_intent("add a morning run to my checklist"), "update")
        self.assertEqual(svc.classify_intent("change the weekday dinner template to light fish and veggies"), "update")

    def test_model_output_is_range_checked(self):
        raw = {
            "meals": {"breakfast": {"foods": [{"name": "Egg", "weight": 50, "cal": 72}]}},
            "exercises": [{"type": "walking", "duration": 30, "steps": 3000,
                           "intensity": "unknown", "kcal": 999999}],
            "sleep": {"bed": "23:30", "wake": "07:00", "quality": 9},
            "weight": 75.6,
            "habits": [0, 0, 99],
        }
        got = svc._sanitize_daily_log(raw, ["breakfast"], 2, ["Walk", "Other"])
        self.assertEqual(got["exercises"][0]["type"], "Walk")
        self.assertEqual(got["exercises"][0]["intensity"], "moderate")
        self.assertEqual(got["exercises"][0]["kcal"], 0)
        self.assertEqual(got["sleep"]["quality"], 5)
        self.assertEqual(got["habits"], [0])
        self.assertEqual(got["weight"], 75.6)

    def test_unknown_exercise_type_falls_back_to_other(self):
        raw = {"exercises": [{"type": "trampoline", "duration": 20}]}
        got = svc._sanitize_daily_log(raw, ["breakfast"], 0, ["Walk", "Run", "Other"])
        self.assertEqual(got["exercises"][0]["type"], "Other")

    def test_pounds_copied_through_by_the_model_are_converted(self):
        """The prompt asks for kg, but a model sometimes returns "182.4 lb" as 182.4.
        Stored as-is it reads as 182 kg (400 lb); it has to come back as about 82.7 kg."""
        got = svc._sanitize_daily_log({"body_kg": 182.4}, ["breakfast"], 0, ["Walk"],
                                      text="weighed 182.4 lb this morning")
        self.assertEqual(got.get("weight"), lb(182.4))

    def test_converted_kilograms_are_left_alone(self):
        """The model did its job (182.4 lb -> 82.74 kg): no second conversion."""
        got = svc._sanitize_daily_log({"body_kg": 82.74}, ["breakfast"], 0, ["Walk"],
                                      text="weighed 182.4 lb this morning")
        self.assertEqual(got.get("weight"), 82.74)

    def test_kilograms_stated_by_the_user_are_not_converted(self):
        got = svc._sanitize_daily_log({"body_kg": 82.5}, ["breakfast"], 0, ["Walk"],
                                      text="82.5 kg today")
        self.assertEqual(got.get("weight"), 82.5)

    def test_past_weights_in_pounds_are_converted_and_future_dropped(self):
        y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        t = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        raw = {"weights": [{"date": y, "kg": 180.2}, {"date": t, "kg": 80.0}]}
        got = svc._sanitize_daily_log(raw, ["breakfast"], 0, ["Walk"],
                                      text="yesterday 180.2 lb, tomorrow I'll be 176.4 lb")
        self.assertEqual(got.get("weights"), [{"date": y, "kg": lb(180.2), "approx": False}])

    def test_food_grams_are_not_taken_as_body_weight(self):
        """A model once put a food's grams into the top-level weight."""
        raw = {"meals": {"lunch": {"foods": [{"name": "Chicken breast", "weight": 150, "cal": 248}]}},
               "weight": 150}
        got = svc._sanitize_daily_log(raw, ["lunch"], 0, ["Walk"],
                                      text="lunch was 150 g grilled chicken breast")
        self.assertNotIn("weight", got)
        self.assertEqual(got["meals"]["lunch"]["cal"], 248)

    def test_ai_update_refuses_daily_logs(self):
        for text in ["log a workout: walked 30 minutes, 3000 steps",
                     "had two eggs and a glass of soy milk for breakfast"]:
            got = svc.llm_update_content(text)
            self.assertFalse(got["ok"], text)
            self.assertEqual(got.get("route"), "record", text)

    def test_unified_parser_returns_five_kinds_of_record(self):
        response = json.dumps({
            "meals": {"breakfast": {"foods": [{"name": "Egg", "weight": 50, "cal": 72}]}},
            "exercises": [{"type": "Walk", "duration": 30, "steps": 3000,
                           "intensity": "moderate", "kcal": 0}],
            "sleep": {"bed": "23:30", "wake": "07:00", "quality": 4},
            "body_kg": 75.6,
            "habits": [0],
        })
        with mock.patch.object(svc, "ai_configured", return_value="test"), \
                mock.patch.object(svc, "_call_llm_text", return_value=response):
            # the text has to mention weight for a body weight to count
            got = svc.daily_log_from_text(
                "log everything for today, weight 75.6 kg", [{"key": "breakfast", "name": "Breakfast"}],
                ["Glass of water when I wake up"], ["Walk", "Other"])
        self.assertTrue(got["ok"])
        self.assertEqual(set(got["log"]), {"meals", "exercises", "sleep", "weight", "habits"})
        self.assertEqual(got["log"]["exercises"][0]["steps"], 3000)
        self.assertEqual(got["log"]["weight"], 75.6)

    def test_unified_parser_converts_pounds_the_model_copied(self):
        response = json.dumps({"body_kg": 166.6})
        with mock.patch.object(svc, "ai_configured", return_value="test"), \
                mock.patch.object(svc, "_call_llm_text", return_value=response):
            got = svc.daily_log_from_text("weighed in at 166.6 lb", None, [], ["Walk", "Other"])
        self.assertTrue(got["ok"])
        self.assertEqual(got["log"]["weight"], lb(166.6))


def read_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class TestAtomicWrite(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_content_correct_and_no_temp_files_left(self):
        p = os.path.join(self.d, "t.json")
        svc.atomic_write(p, '{"a": 1}')
        self.assertEqual(read_json(p), {"a": 1})
        self.assertEqual([f for f in os.listdir(self.d) if f.endswith(".tmp")], [])

    def test_overwrite_never_leaves_half_a_file(self):
        p = os.path.join(self.d, "t.json")
        svc.atomic_write(p, '{"a": 1}')
        svc.atomic_write(p, '{"b": 2}')
        self.assertEqual(read_json(p), {"b": 2})


class TestRecognitionParsing(unittest.TestCase):

    def test_normal_reply(self):
        # note: the key agreed in the prompt is "foods", not "items"
        r = svc.parse_foods('{"foods":[{"name":"White rice","weight":150,"cal":195}],"total":195}')
        self.assertEqual(r[1], 195)
        self.assertEqual(r[2], "White rice")

    def test_falls_back_to_a_single_calorie_number(self):
        r = svc.parse_foods("This plate is about 350 kcal")
        self.assertEqual(r[1], 350)
        r = svc.parse_foods("Roughly 420 calories in total")
        self.assertEqual(r[1], 420)

    def test_bad_replies_do_not_raise(self):
        for bad in ["", "not JSON", "{}", None]:
            try:
                svc.parse_foods(bad)
            except Exception as e:
                self.fail("parse_foods(%r) raised %s" % (bad, e))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
