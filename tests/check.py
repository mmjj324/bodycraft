"""Change guard. Run it right after every front-end edit, so a big search-and-replace can't do silent damage.

Once a block was replaced using s.index() to find its range; the offset was wrong, a few thousand
lines were cut in one go, and all 18 editors disappeared -- nobody noticed until assertions much
later failed. With this script it shows up on the spot.
"""
import io, re, sys, subprocess, os, shutil, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the folder above tests/
HTML = os.path.join(BASE, "bodycraft.html")

# Functions that must exist (one missing means something got cut)
FUNCS = [
    # editors and panels
    "openListEditor", "openScheduleEditor", "openHabitsEditor", "openMenuEditor",
    "openPlateEditor", "openRulesEditor", "openPlateTipsEditor", "openDailyTipsEditor",
    "openGoalsEditor", "openExTypesEditor", "openTypeMetaEditor", "openMealsEditor",
    "openMilestoneEditor", "openCoachEditor", "openRemindersEditor",
    "openImportPanel", "openWeekReview", "openMealTextPanel", "openDailyLogPanel", "suspendCoachForModal", "saveContentPatch",
    # setup wizard and guards
    "runFirstSetup", "runWizard", "checkGoal", "paceHint", "bindPaceHint",
    "generateTargets", "achievements", "planMilestones",
    # rendering
    "renderTimeline", "renderChecklist", "renderWater", "renderCalSegs", "renderNetCal",
    "renderMenu", "renderRules", "renderPlateTips", "renderWeightAll", "renderMilestones",
    "renderPhotos", "renderBodyToday", "renderMemberBar", "renderProfileUI",
    "renderDailyTip", "renderLegend", "renderAchievements", "renderExTypes",
    "renderCoachButtons", "refreshDerived", "updateErrCount", "showErrLog", "bindAskFood", "renderIdeas", "mealCtx", "renderFavs", "loadFavs", "eatFav", "bindFavAdd", "bindFavUrl", "bindLookup", "loadLookupTable", "bindSelfie", "renderHero", "heroGreeting", "todayRecordCount", "daysSinceLastRecord", "loadSelfies", "renderSelfies", "openKnowledgePanel", "openWipePanel", "clearLocalData", "localDataSummary", "updateNowCard", "highlightNow", "updateStats",
    # interaction
    "showConfirmPanel", "pickPhoto",
    "coachRespond", "openCoach", "bindCoach", "applyCoachSize", "cycleCoachSize", "coachTips", "localCoachSay", "todaySnapshot", "bindDayCheck", "coachOutsideClick", "bindCoachTips",  "openAI", "syncHabitExercise", "latestWeight", "confirmExercise", "recordExercise",
    # eating window and first-weeks guidance
    "ewGet", "ewNow", "ewWeek", "ewIn", "renderFastCard", "openEatWindow", "loadMealTimes", "mealsWithTime",
    "renderEatTimes", "renderWeightBrief", "knowCards", "knowMatch", "renderKnowCards", "openKnowCard",
    "coachLocalNote", "coachOfferRecord", "weighInNote", "fiveTxt", "renderDiaryMember",
    "ewFor", "ewWithChange", "saveEatWindow", "eatingDays", "circMean", "ewTip", "openHealthNote", "coachOfferHealth",
    "buildSchedule", "addDays", "dayDiff", "normalizeHeight", "toKg",
    # pounds and kilograms, feet and inches
    "weightUnit", "wShort", "fromKg", "wVal", "fmtW", "fmtWDelta", "fmtH", "cmToFtIn", "ftInToCm",
    "safeWeeklyLoss", "bodyOf", "budgetFloor", "hasPlan", "startWeightOf", "renderPlanEnd", "readImageForUpload", "applyFontScale", "bindWeightUnit", "activeSchedule", "renderScheduleTabs", "weekendFromWorkday", "isRestDay", "swapShift", "ensureSupperSlot", "openScheduleBuilder", "shiftNow", "isNightSchedule",
    "foodMacros", "mealMacros", "macrosToday", "dailyBudget", "macroTargets", "renderMacros", "hasMacros", "coachSendImage", "coachTakeImage", "renderCoachAttach", "mealKeyByHour",
    "exKcal", "habitExercise", "calcBMR", "calcBurn", "activityInfo", "mealEffective", "renderBudget", "renderInsights", "renderLostCard", "lookupCacheFind", "lookupCalcCalories", "renderLookupCalculator", "bindLookupCalculator", "updateLookupCalcNumbers", "renderCoachChips", "showBudgetHow", "dailyDeficitFromPlan", "intakeToday", "mealDefaultKcal", "editMealDefault", "dayHasRecord", "renderPeriodMark", "togglePeriod", "isPeriodDay", "periodDays", "exList", "exTotal", "exMinutes", "exPush", "exRemove", "nowHM", "renderDiaryTrail", "deleteDiary", "refreshDensityHint", "openColorPicker", "attachEditMenu", "setupEditMenus",   "esc", "safe", "toast", "attachVoice",
]

# DOM ids that must exist
IDS = [
    "heroTitle", "heroBadge", "statWeight", "statLoss", "statKcal",
    "checklist", "cups", "calSegs", "netCalBar", "photoSlots",
    "exType", "exDur", "exIntensity", "exSave", "exToday",
    "sleepSave", "wDate", "wSave", "weightViewBar", "milestones", "weekTable",
    "timeline", "legend", "rulesGrid", "plateTipsGrid",
    "fastCard", "weightBrief", "eatTimes", "eatTimesCard", "knowCards", "healthNoteBtn",
    "scheduleTitle", "scheduleTabs", "planEndBar", "fontBtn", "wUnitBtn", "s0Unit", "s0Ft", "s0In", "s0Height", "wSex", "wAge", "wAct", "wUnit", "s0WWake", "scheduleAltBar", "rebuildScheduleBtn", "s0M1", "s0M2", "s0M3", "macroBar", "llmFastModel", "llmModel", "llmVisionModel", "coachImgBtn", "coachImgFile", "coachAttach",
    "memberChips", "coachPanel", "coachBody", "coachInput", "coachSend", "coachComposer",
    "aiUpdateBtn", "apikeyBtn", "resetupBtn", "errCount", "statBudgetCard", "askFoodGoAll", "statLostCard", "coachChips", "statLostLabel", "insights", "statKcalLabel", "wipeBtn", "importBtn",
    "editScheduleBtn", "editTypesBtn", "editHabitsBtn", "editGoalsBtn",
    "editTipsTextBtn", "editRulesBtn", "editExTypesBtn",
    "editMealsBtn", "editMilestoneBtn", "editCoachBtn", "editRemindersBtn",
    "weekReviewBtn", "reviewBtn", "mealTextBtn", "diaryText", "diaryTrail", "diaryDelBtn", "askFoodText", "askFoodOut", "ideaBreakfast", "ideaLunch", "ideaDinner", "favList", "selfieAddBtn", "selfieGrid", "lkCalcRows", "lkCalcAdd", "lkCalcClear", "lkCalcTotal",
]


def check(verbose=True):
    s = io.open(HTML, encoding="utf-8").read()
    problems = []

    for f in FUNCS:
        if not re.search(r"\bfunction\s+%s\s*\(" % re.escape(f), s):
            problems.append("missing function: " + f)
    for i in IDS:
        if ('id="%s"' % i) not in s:
            problems.append("missing element: #" + i)

    # JS syntax
    js = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", s))
    # a temp file per run: two checks running side by side must not overwrite each other's copy
    fd, tmp_js = tempfile.mkstemp(prefix="bodycraft_check_", suffix=".js")
    try:
        with io.open(fd, "w", encoding="utf-8") as f:
            f.write(js)
        if shutil.which("node"):
            r = subprocess.run(["node", "--check", tmp_js], capture_output=True)
            if r.returncode != 0:
                problems.append("JS syntax: " + r.stderr.decode()[:200])
        elif verbose:
            print("  (node not installed; JS syntax check skipped)")
    finally:
        os.remove(tmp_js)

    # HTML parses
    from html.parser import HTMLParser
    try:
        HTMLParser().feed(s)
    except Exception as e:
        problems.append("HTML parse: %s" % e)

    # duplicate ids
    import collections
    ids = re.findall(r'\sid="([A-Za-z][\w-]*)"', s)
    dup = {k: v for k, v in collections.Counter(ids).items() if v > 1}
    if dup:
        problems.append("duplicate ids: %s" % dup)

    size = len(s.encode()) / 1024
    if verbose:
        if problems:
            print("FAIL: %d problem(s) (%.0f KB):" % (len(problems), size))
            for p in problems:
                print("   -", p)
        else:
            print("Integrity OK (%d functions / %d elements / %.0f KB)" % (len(FUNCS), len(IDS), size))
    return problems


if __name__ == "__main__":
    sys.exit(1 if check() else 0)
