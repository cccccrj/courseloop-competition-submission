"""备考时间预算 —— 「这个人这一天，能给这门课多少分钟」。

## 为什么要有这一层

原先排期只有一个全局 `daily_minutes`（spaced_review.build_plan），意味着：周三满课
和周六整天空在系统眼里一模一样，实习/期末周这种整段没时间的日子根本无法表达。
于是所有人拿到的都是同一形状的计划 —— 每天等量、填到满、一路填到考试。

这个模块把「容量」从一个常数变成一个按日期查询的函数，排期算法据此排。

## 三条约定

1. **预算按人存，按 408 分值分摊到课。** 一个人一天只有那么多时间，四门课共享它。
   分摊权重直接用 courses.Course.score（45/45/35/25），既是现成数据，也正好是
   「时间该往哪门课倾斜」的正确答案 —— review_planner 的提示词早就在用同一个权重。

2. **留白发生在这里，不在排期算法里。** day_minutes() 返回的已经是打过折的
   「可排分钟」，排期算法只管把它填满。这样"不排满"这件事只有一个实现位置，
   调参也只调一处。

3. **机动日返回 0，补做日不返回 0。** 新任务排不进机动日（day_minutes），
   但落后后的补做可以用它（catchup_minutes）。没落后的时候它就是真的休息日 ——
   这是它与"预算填 0 的那天"唯一的区别。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from math import gcd

from backend.core import db
from backend.core.courses import all_courses, get_course

# 周模板默认值 = 前端「适中」档的推荐值（周一到周六各 8 小时，周日休息）。
#
# 这两处**必须逐项相同**（tests/test_frontend_constants.py 钉住）：没填过预算的人
# 拿到的就是 (intensity=medium, DEFAULT_WEEKLY)，而界面上「适中」这一档写着自己
# 推荐多少小时。两边不一致的话，一个什么都没动的人会看到档位说 8 小时、格子里却是
# 别的数字，界面还会告诉他"你已手动调整过"——他并没有。
DEFAULT_WEEKLY = [480, 480, 480, 480, 480, 480, 0]

# 强度 → 填充率。上限 0.85 而不是 1.0：**没有任何一档会把时间排满。**
# 冲刺期的人也需要吃饭、通勤和从一件事切到另一件事的成本，
# 按 100% 排出来的计划第一天就会欠账，然后一路欠下去。
FILL_RATIO = {"light": 0.60, "medium": 0.72, "sprint": 0.85}
DEFAULT_INTENSITY = "medium"

HORIZON_DAYS = 14        # 滚动窗口：只详细排未来两周，更远的按周给配额
MAX_DEBT_PER_DAY = 2     # 单日补做上限，债务不允许无限堆积

# 必做/选做的分层参数在 spaced_review（CORE_SHARE / MAX_CORE_PER_DAY）——
# 那是排期算法的策略，不是预算的属性。这里只负责"有多少分钟"。

# 单日预算上限，防御脏输入。16 小时与前端 CLWeekBudget 的 MAX_HOURS 对齐——
# 低于它的话，界面允许填 16 小时、保存时却被静默钳到 12，用户看到的和存下来的是
# 两个数，而且没有任何提示。「冲刺」档就是每天 16 小时，这里卡住它等于那一档存不住。
_MAX_DAILY = 960
_DEFAULT_TASK_MINUTES = 15
_TASK_MINUTES_RANGE = (8, 30)


def course_share(course: str) -> float:
    """该门课在总预算里的占比 = 本门 408 分值 / 四门分值和。"""
    total = sum(c.score for c in all_courses().values())
    return get_course(course).score / total if total else 1.0


# ── 课程轮值 ──────────────────────────────────────────────────────────
#
# 按分值把每一天**切成四份**，在小预算下会切出谁都用不了的碎片：一天 60 分钟，
# 数据结构分到 60×0.30×0.72 = 12 分钟，连一道题都装不下，于是这门课一项也排不出来。
# 更糟的是它只惩罚如实填写的人 —— 没填过预算的人 share 取 1.0（见 review_service
# ._budget_for），拿到的是每天 43 分钟的完整计划；老老实实填了"我一天 60 分钟"的人，
# 计划反而是空的。
#
# 真实的学习也不是切碎的：没有人会在一天里给四门课各 12 分钟。所以当分摊后的份额
# 连一项任务都装不下时，改为**按天轮值** —— 这一天整天归一门课，四门按 408 分值轮流。
#
# 轮值表用 D'Hondt 顺序分配（每步选 分值/(已得+1) 最大的那门），得到的序列既符合
# 分值比例，又不会让某门课连续缺席太久。分值 45/45/35/25 的周期是 30 天，四门分别
# 占 9/9/7/5 天。当天的庄家由 date.toordinal() 取模决定：纯函数、不依赖任何存储，
# 四门课各自排各自的期，既不会撞在同一天，也不会漏掉哪一天。


def _rotation_cycle() -> list[str]:
    courses = all_courses()
    ids = list(courses)
    scores = [courses[c].score for c in ids]
    total = sum(scores)
    if not total:
        return ids
    step = gcd(*scores) if len(scores) > 1 else scores[0]
    length = total // (step or 1)
    got = [0] * len(ids)
    cycle: list[str] = []
    for _ in range(length):
        # 同分时取靠前的那门（courses 表 sort_order 的登记顺序），保证结果可复现。
        pick = max(range(len(ids)), key=lambda i: (scores[i] / (got[i] + 1), -i))
        got[pick] += 1
        cycle.append(ids[pick])
    return cycle


ROTATION_CYCLE = _rotation_cycle()


def day_owner(d: date) -> str:
    """这一天轮到哪门课坐庄。"""
    return ROTATION_CYCLE[d.toordinal() % len(ROTATION_CYCLE)]


def default_budget() -> dict:
    return {"weekly_minutes": list(DEFAULT_WEEKLY), "intensity": DEFAULT_INTENSITY,
            "buffer_weekday": 6, "exam_date": "", "exceptions": [], "is_default": True}


def normalize(raw: dict | None) -> dict:
    """把库里的行（weekly_minutes 是 JSON 字符串）整理成排期能直接吃的形状。

    任何一项脏值都回落到默认值而不是抛错：预算是用户随手填的，
    一个手滑不该让整个复习计划打不开。
    """
    if not raw:
        return default_budget()
    try:
        weekly = json.loads(raw.get("weekly_minutes") or "[]")
    except (ValueError, TypeError):
        weekly = []
    if not isinstance(weekly, list) or len(weekly) != 7:
        weekly = list(DEFAULT_WEEKLY)
    weekly = [_clamp_minutes(v) for v in weekly]

    intensity = raw.get("intensity") or DEFAULT_INTENSITY
    if intensity not in FILL_RATIO:
        intensity = DEFAULT_INTENSITY
    try:
        buffer_weekday = int(raw.get("buffer_weekday", 6))
    except (ValueError, TypeError):
        buffer_weekday = 6
    if not -1 <= buffer_weekday <= 6:
        buffer_weekday = -1

    return {"weekly_minutes": weekly, "intensity": intensity,
            "buffer_weekday": buffer_weekday, "exam_date": raw.get("exam_date") or "",
            "exceptions": [dict(e) for e in (raw.get("exceptions") or [])],
            "is_default": False}


def _clamp_minutes(value) -> int:
    try:
        return max(0, min(_MAX_DAILY, int(value)))
    except (ValueError, TypeError):
        return 0


def load(username: str) -> dict:
    """取该生预算；没填过返回默认档（带 is_default 标记，供界面提示"这是默认值"）。"""
    return normalize(db.get_study_budget(username))


def _exception_map(budget: dict) -> dict[str, int]:
    return {e["date"]: _clamp_minutes(e["minutes"]) for e in budget.get("exceptions", [])}


def raw_day_minutes(budget: dict, d: date, exceptions: dict[str, int] | None = None) -> int:
    """当天的**原始**可用分钟（未分摊、未打折、不看机动日）。界面上展示的就是这个数。"""
    exc = _exception_map(budget) if exceptions is None else exceptions
    key = d.isoformat()
    if key in exc:
        return exc[key]
    return budget["weekly_minutes"][d.weekday()]


def day_minutes(budget: dict, d: date, share: float = 1.0,
                exceptions: dict[str, int] | None = None,
                *, course: str = "", min_task: int = 0) -> int:
    """某天分给这门课的**可排新任务**分钟数。

    优先级：机动日(0) > 例外日 > 周模板；再乘课程占比与填充率。
    机动日排在最前 —— 用户为它挑了一个具体的周几，这个意图不该被周模板的数字盖过。

    course/min_task 同时给出时启用轮值兜底（见上方 ROTATION_CYCLE 的说明）；
    不给就是原来的纯比例分摊，老调用方行为不变。
    """
    if budget["buffer_weekday"] == d.weekday():
        return 0
    return catchup_minutes(budget, d, share, exceptions, course=course, min_task=min_task)


def catchup_minutes(budget: dict, d: date, share: float = 1.0,
                    exceptions: dict[str, int] | None = None,
                    *, course: str = "", min_task: int = 0) -> int:
    """某天可用于**补做**的分钟数。与 day_minutes 只差一件事：机动日在这里是有容量的。"""
    raw = raw_day_minutes(budget, d, exceptions)
    if raw <= 0:
        return 0
    fill = FILL_RATIO[budget["intensity"]]
    portion = int(raw * share * fill)
    if not course or min_task <= 0 or portion >= min_task:
        return portion
    # 分摊后连一项任务都装不下：这天不再切四份，整天归轮值到的那门课。
    return int(raw * fill) if day_owner(d) == course else 0


def day_caps(budget: dict, start: date, end: date, share: float = 1.0,
             *, course: str = "", min_task: int = 0) -> dict[str, int]:
    """[start, end] 闭区间每天的可排分钟表，作为纯函数排期算法的输入。

    把日期展开在这里做，spaced_review 那边就仍然只吃 {日期: 容量} 这种朴素结构，
    继续保持"不依赖日期库外部状态"（见该模块文件头）。
    """
    exc = _exception_map(budget)
    caps: dict[str, int] = {}
    d = start
    while d <= end:
        caps[d.isoformat()] = day_minutes(budget, d, share, exc, course=course, min_task=min_task)
        d += timedelta(days=1)
    return caps


def per_task_minutes(username: str) -> int:
    """单题预计耗时（分钟），由该生最近的实际作答用时估计。

    原先这个值全局写死 15（spaced_review.build_plan 的默认参数），对做题慢的人
    是系统性高估产能：他一天只能做 2 道，计划却按 4 道排，于是天天欠账。
    库里 practice_attempts.duration 一直在记，只是从来没有人读过。
    """
    seconds = db.attempt_duration_median(username)
    if not seconds:
        return _DEFAULT_TASK_MINUTES
    lo, hi = _TASK_MINUTES_RANGE
    return max(lo, min(hi, round(seconds / 60) or lo))


def weekly_total(budget: dict) -> int:
    """一周原始预算合计，界面上「本周预算 540 分钟」那行用。"""
    return sum(budget["weekly_minutes"])
