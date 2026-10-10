"""间隔复习调度。

基础间隔 1/3/7/14/30 天；按答题表现动态提前（答错/依赖提示→提前）或推迟（独立答对→推迟）。
结合知识点掌握度与考试日期排每日任务。纯函数，不依赖日期库外部状态（天数由调用方传入）。
"""
from __future__ import annotations

BASE_INTERVALS = [1, 3, 7, 14, 30]


def next_interval(stage: int, correct: bool, hints_used: int = 0) -> tuple[int, int]:
    """返回 (下次间隔天数, 新的 stage)。

    stage: 当前处于第几个间隔（0-based）。
    答对且少提示 -> 进阶到更长间隔；答错 -> 回退到最短间隔。
    """
    n = len(BASE_INTERVALS)
    if not correct:
        return BASE_INTERVALS[0], 0
    # 依赖提示则原地巩固，不进阶
    if hints_used >= 2:
        stage = max(0, stage)
    else:
        stage = min(stage + 1, n - 1)
    return BASE_INTERVALS[stage], stage


# 没练过的知识点，其 mastery=0 是"不知道"而不是"确实不会"。不打这个折的话，
# 它与"练了但答错"的点同为满分 2.0 —— 于是刚做错的题排不到从没打开过的章节前面，
# "根据错题推荐练习"这件事就不成立了。有过错题就是**有证据的**薄弱，该排更前。
UNEXPLORED_DISCOUNT = 0.75


def priority(mastery: float, error_freq: int, days_since: float, is_prereq: bool = False,
             attempts: int = 1) -> float:
    """复习优先级评分（越大越该先复习）。

    attempts 默认 1（视作已探索）：老调用方不传时行为与改动前一致。
    """
    base = (1 - mastery) * 2.0
    if attempts <= 0 and error_freq <= 0:
        base *= UNEXPLORED_DISCOUNT
    score = base + error_freq * 0.5 + max(0.0, days_since - 7) * 0.05
    if is_prereq:
        score += 0.5
    return round(score, 3)


def rank_key(k: dict):
    """排序键。同分时按大纲章节顺序 —— 冷启动全门同分，从第一章开始才合理。

    profile.build_profile 与 build_plan 必须用同一把尺子，否则会出现"复习计划说
    先补树，练习页却一直推排序"这种没法解释的不一致。
    """
    return (-priority(k.get("mastery", 0) or 0, k.get("error_freq", 0) or 0,
                      k.get("days_since", 0) or 0, k.get("is_prereq", False),
                      k.get("attempts", 1) or 0),
            k.get("chapter_index", 99), k.get("knowledge_id", 0))


def task_shape(k: dict) -> tuple[str, int, str]:
    """由掌握度决定这个知识点该做什么、做几道、为什么。两个排期器共用。"""
    m = k.get("mastery", 0)
    if m < 0.4:
        task_type, count, reason = "讲解+基础练习", 3, "掌握度低，需重新学习"
    elif m < 0.7:
        task_type, count, reason = "针对性练习", 4, "掌握不稳，强化巩固"
    else:
        task_type, count, reason = "错题重做", 2, "接近掌握，查漏补缺"
    if k.get("is_prereq"):
        reason += "（后续章节的前置知识）"
    return task_type, count, reason


def build_plan(knowledge_states: list[dict], days_to_exam: int,
               daily_minutes: int = 60, per_task_minutes: int = 15,
               repeat: bool = True, max_reviews: int = 3) -> list[dict]:
    """由知识点状态生成每日复习任务。

    knowledge_states: [{knowledge_id, name, mastery, error_freq, days_since, is_prereq}]
    返回 review_tasks 列表：[{day, knowledge_id, name, task_type, target_count, minutes, reason}]
    """
    ranked = sorted(knowledge_states, key=rank_key)
    days_to_exam = max(1, int(days_to_exam))
    tasks_per_day = max(1, int(daily_minutes) // per_task_minutes)
    capacity = {day: tasks_per_day for day in range(1, days_to_exam + 1)}
    plan: list[dict] = []

    def first_day(start: int) -> int | None:
        for candidate in range(max(1, start), days_to_exam + 1):
            if capacity[candidate] > 0:
                return candidate
        return None

    for k in ranked:
        task_type, count, reason = task_shape(k)
        stage = max(0, min(len(BASE_INTERVALS) - 1, int(k.get("review_stage", 0) or 0)))
        days_since = float(k.get("days_since", 0) or 0)
        start = max(1, BASE_INTERVALS[stage] - int(days_since))
        rounds = max_reviews if repeat else 1
        for round_i in range(rounds):
            day = first_day(start)
            if day is None:
                break
            capacity[day] -= 1
            is_followup = round_i > 0
            current_count = max(1, count // 2) if is_followup else count
            current_type = "错题重做 + 自测" if is_followup else task_type
            interval, next_stage = next_interval(stage, correct=True, hints_used=0)
            plan.append({"day": day, "knowledge_id": k.get("knowledge_id"), "name": k.get("name"),
                         "task_type": current_type, "target_count": current_count,
                         "minutes": per_task_minutes, "reason": reason,
                         "round": round_i + 1, "stage": stage, "interval": interval,
                         "priority": k.get("priority", 0),
                         "error_type": k.get("top_error_type", "")})
            stage = next_stage
            start = day + interval
    return plan


# ── 按时间预算的滚动排期 ──────────────────────────────────────────────
#
# 与上面 build_plan 的三处不同，也就是"不排满"这件事的全部实现：
#
#   1. 容量按天不同（day_caps 传进来），且是分钟而不是"几项"；
#   2. 每天拆成必做/选做两个桶，做完必做当天就算完成；
#   3. 只排 day_caps 覆盖的那段窗口，排不下的不硬塞，交给里程碑按周给配额。
#
# 仍然是纯函数：不 import datetime，不知道"今天"是几号。日期只以字符串形式
# 从 day_caps 的键流进来、从 task_date 流出去，天数差以偏移量表达（due_offset_days）。

CORE_SHARE = 0.6        # 必做占当天可排分钟的比例
MAX_CORE_PER_DAY = 3    # 「今日必做」最多几项 —— 超过 3 项就不再是"必做"了
DUE_SLACK_RATIO = 0.25  # 到期窗口宽度 = 间隔 × 这个比例，最少 1 天


def build_windowed_plan(knowledge_states: list[dict], day_caps: dict[str, int],
                        weeks: list[dict] | None = None, *,
                        per_task_minutes: int = 15, max_reviews: int = 3,
                        skip_knowledge_ids: set[int] | None = None) -> dict:
    """按每日分钟预算排期。

    day_caps: {"2026-08-29": 43, ...} —— 该天可排**新任务**的分钟数（已含留白折扣）。
              ISO 日期串的字典序即时间序，且必须是连续的一段，索引差 = 天数差。
    weeks:    窗口之外的周配额 [{"week_start","week_end","minutes"}]，用于生成里程碑。
    skip_knowledge_ids: 续排时排除已有未完成任务的知识点，避免同一个点排两遍。

    返回 {"tasks": [...], "milestones": [...]}。task 里的 due_offset_days 是
    相对 task_date 的容差天数，由调用方换算成 due_to —— 本模块不做日期加法。
    """
    dates = sorted(day_caps)
    if not dates:
        return {"tasks": [], "milestones": []}
    skip = skip_knowledge_ids or set()

    # 容量按"当天总剩余分钟"记，必做只是它上面的一条软上限 —— 不能把一天**切成**
    # 两个各自独立的桶。切开的话，容量落在 [per_task, per_task/CORE_SHARE) 区间的
    # 那些天会两个桶都装不下一项，于是整天排空：默认档（工作日 60 分钟，四门课按 408
    # 分值分摊后单科只剩 12~18 分钟）几乎每个工作日都落在这个区间里，两周排出来
    # 只有两项任务。用户明明给了时间，系统却说"今天没有安排"，这是最不该出现的输出。
    #
    # 所以必做上限取 max(一项, CORE_SHARE) ——「每个放得下一项任务的日子，至少有一件必做」
    # 优先于「必做不超过六成」。零散的日子本来也只排得下一项，那一项理应是必做的那件。
    left = {d: day_caps[d] for d in dates}
    core_cap = {d: max(per_task_minutes, round(day_caps[d] * CORE_SHARE))
                if day_caps[d] >= per_task_minutes else 0 for d in dates}
    core_used = {d: 0 for d in dates}
    # core 额外受 MAX_CORE_PER_DAY 限制 —— 光靠分钟数约束不住"项数"，
    # 而"今天有 7 件必做的事"本身就是压垮人的那个东西，与总时长无关。
    core_count = {d: 0 for d in dates}

    def place(from_idx: int) -> tuple[int, str] | None:
        """从 from_idx 起找第一个放得下的日子，返回 (索引, tier)。"""
        for i in range(max(0, from_idx), len(dates)):
            d = dates[i]
            if left[d] < per_task_minutes:
                continue
            if (core_count[d] < MAX_CORE_PER_DAY
                    and core_used[d] + per_task_minutes <= core_cap[d]):
                return i, "core"
            return i, "optional"
        return None

    tasks: list[dict] = []
    leftover: list[dict] = []

    for k in sorted(knowledge_states, key=rank_key):
        if int(k.get("knowledge_id") or 0) in skip:
            continue
        task_type, count, reason = task_shape(k)
        stage = max(0, min(len(BASE_INTERVALS) - 1, int(k.get("review_stage", 0) or 0)))
        days_since = float(k.get("days_since", 0) or 0)
        # 0-based 偏移。build_plan 那边的 day 是 1-based，同一个式子搬过来要减 1，
        # 否则从没练过的点（stage=0 → 间隔 1 天）会整体后移一天，窗口第一天空着。
        cursor = max(0, BASE_INTERVALS[stage] - int(days_since) - 1)

        placed_any = False
        for round_i in range(max(1, max_reviews)):
            slot = place(cursor)
            if slot is None:
                break
            idx, tier = slot
            d = dates[idx]
            left[d] -= per_task_minutes
            if tier == "core":
                core_used[d] += per_task_minutes
                core_count[d] += 1
            placed_any = True

            is_followup = round_i > 0
            interval, next_stage = next_interval(stage, correct=True, hints_used=0)
            tasks.append({
                "day": idx + 1, "task_date": d,
                "knowledge_id": k.get("knowledge_id"), "name": k.get("name"),
                "task_type": "错题重做 + 自测" if is_followup else task_type,
                "target_count": max(1, count // 2) if is_followup else count,
                "minutes": per_task_minutes, "reason": reason,
                "round": round_i + 1, "stage": stage, "interval": interval,
                "priority": k.get("priority", 0), "error_type": k.get("top_error_type", ""),
                # 第 2 轮起一律 optional：巩固轮次是"有余力再做"，
                # 把它摆在必做位上，等于让复习的深度去挤复习的广度。
                "tier": "core" if (tier == "core" and not is_followup) else "optional",
                "due_from": d,
                "due_offset_days": max(1, round(interval * DUE_SLACK_RATIO)),
            })
            stage = next_stage
            cursor = idx + interval

        if not placed_any:
            leftover.append(k)

    return {"tasks": tasks,
            "milestones": _milestones(leftover, weeks or [], per_task_minutes)}


def _milestones(leftover: list[dict], weeks: list[dict], per_task_minutes: int) -> list[dict]:
    """窗口之外只给周级配额，不生成具体某天的任务行。

    排一份 200 天的精确表在第 3 天就作废了 —— 中间任何一次没做完都会让后面全错位，
    而它偏偏又长得像个承诺。按周给"这周补图和排序，约 6 小时"这种粒度，
    既说清了节奏，又不假装知道你第 87 天上午干什么。
    """
    if not leftover or not weeks:
        return []
    out: list[dict] = []
    cursor = 0
    for w in weeks:
        budget = max(0, int(w.get("minutes") or 0))
        if budget < per_task_minutes or cursor >= len(leftover):
            continue
        take = min(len(leftover) - cursor, budget // per_task_minutes)
        if take <= 0:
            continue
        chunk = leftover[cursor:cursor + take]
        cursor += take
        chapters: list[str] = []
        for k in chunk:
            ch = k.get("chapter") or ""
            if ch and ch not in chapters:
                chapters.append(ch)
        out.append({
            "week_start": w["week_start"], "week_end": w["week_end"],
            "chapters": chapters,
            "points": [{"knowledge_id": k.get("knowledge_id"), "name": k.get("name")}
                       for k in chunk],
            "point_count": take, "minutes": take * per_task_minutes,
        })
    return out
