"""复习计划应用服务：画像 → 排期 → 持久化 → 打卡/动态重排。

排期有两条路径，共用同一份持久化与判题回流：

- **预算路径**（默认）：按 study_budget 的周模板/例外日算出每天的可排分钟，
  只详细排未来两周，剩下的按周给里程碑；每天分必做/选做两层；任务带到期窗口。
  落后由 reconcile() 自愈。
- **定额路径**（`use_budget=False`）：原来的 build_plan，每天等量填满到考试日。
  保留它不是为了兼容测试，而是因为"我就想要一张满课表"是个合法诉求。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from backend.agents import review_planner
from backend.core import db
from backend.learning import profile, spaced_review, study_budget

# 债务必须在这么多天内还完。给得太长（比如整个滚动窗口 14 天）等于允许无限欠账，
# 因为新任务同时还在往后排；给得太短则一天要补五六项，又回到"压垮人"那一边。
DEBT_HORIZON_DAYS = 5


def _date(value: str, fallback: date | None = None) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        if fallback is not None:
            return fallback
        raise ValueError("日期必须是 YYYY-MM-DD")


def _decorate(plan: dict | None, username: str, course: str) -> dict | None:
    if not plan:
        return None
    plan["progress"] = db.review_progress(username, course) or {}
    kids = {int(t["knowledge_id"]) for t in plan.get("tasks", []) if t.get("knowledge_id")}
    if kids:
        rows = [r for r in profile.build_profile(username, course) if r["knowledge_id"] in kids]
        current = sum(float(r["mastery"] or 0) for r in rows) / len(kids)
        plan["progress"]["mastery_delta"] = round(
            current - float(plan.get("baseline_mastery") or 0), 4)
    try:
        plan["milestones"] = json.loads(plan.get("milestones") or "[]")
    except (ValueError, TypeError):
        plan["milestones"] = []
    return plan


def _budget_for(username: str, course: str, daily_minutes: int) -> tuple[dict, float, str]:
    """返回 (预算, 该门课的占比)。

    没填过预算时，用请求里的 daily_minutes 铺成一张平的周模板，且**占比取 1.0** ——
    那个数字是用户在某一门课的页面上填的，意思是"这门课每天 X 分钟"，
    再乘一次分值权重就变成了他没要求过的东西。只有真正填过全局预算的人，
    才谈得上"把总时间按 408 分值分给四门课"。
    """
    budget = study_budget.load(username)
    if budget["is_default"]:
        # share=1.0 意味着这一天整天就是这门课的，本来就不存在"切碎"问题，
        # 轮值也就无从谈起 —— 课程 id 返空，明确不参与轮值。
        return {**budget, "weekly_minutes": [daily_minutes] * 7, "buffer_weekday": -1}, 1.0, ""
    return budget, study_budget.course_share(course), course


def _weeks_between(start: date, end: date, budget: dict, share: float,
                   course: str = "", min_task: int = 0) -> list[dict]:
    """把 [start, end] 切成自然周，每周带上该门课在这周的可用分钟。里程碑用。"""
    weeks: list[dict] = []
    cursor = start
    while cursor <= end and len(weeks) < 60:
        week_end = min(end, cursor + timedelta(days=6 - cursor.weekday()))
        minutes = sum(study_budget.catchup_minutes(budget, cursor + timedelta(days=i), share,
                                                   course=course, min_task=min_task)
                      for i in range((week_end - cursor).days + 1))
        weeks.append({"week_start": cursor.isoformat(), "week_end": week_end.isoformat(),
                      "minutes": minutes})
        cursor = week_end + timedelta(days=1)
    return weeks


def create_plan(username: str, course: str, *, start_date: str, exam_date: str,
                daily_minutes: int = 60, goal: str = "", max_points: int = 20,
                use_budget: bool = True) -> dict:
    if not username:
        raise ValueError("未登录")
    start = _date(start_date, date.today())
    exam = _date(exam_date)
    if exam < start:
        raise ValueError("考试日期不能早于计划开始日期")
    daily_minutes = max(15, min(480, int(daily_minutes or 60)))
    max_points = max(1, min(50, int(max_points or 20)))
    states = profile.build_profile(username, course, limit=max_points)
    if not states:
        raise ValueError("当前课程没有知识点数据")
    days = max(1, (exam - start).days + 1)

    if use_budget:
        created = _create_budgeted(username, course, states, start, exam, days,
                                   daily_minutes, goal)
    else:
        created = _create_uniform(username, course, states, start, exam, days,
                                  daily_minutes, goal)
    # 回读一次而不是直接返回写入结果：今日/本周视图要和 /review/plan/active
    # 走同一条组装路径，两个端点给出不同形状的 plan 是前端最难查的一类 bug。
    plan = get_active(username, course) or created
    plan["cold_start"] = not any(r["attempts"] or r["error_total"] for r in states)
    return plan


def _create_uniform(username, course, states, start: date, exam: date, days: int,
                    daily_minutes: int, goal: str) -> dict:
    """原来的定额排期：每天等量、填满到考试日。"""
    generated = review_planner.plan(states, days_to_exam=days,
                                    daily_minutes=daily_minutes, goal=goal, course=course)
    for task in generated["tasks"]:
        task["task_date"] = (start + timedelta(days=int(task["day"]) - 1)).isoformat()
    return db.create_review_plan(
        username, course, goal, start.isoformat(), exam.isoformat(), daily_minutes,
        generated.get("summary", ""), generated["tasks"],
        *_baselines(states))


def _create_budgeted(username, course, states, start: date, exam: date, days: int,
                     daily_minutes: int, goal: str) -> dict:
    budget, share, rotate = _budget_for(username, course, daily_minutes)
    per_task = study_budget.per_task_minutes(username)
    horizon_end = min(exam, start + timedelta(days=study_budget.HORIZON_DAYS - 1))
    caps = study_budget.day_caps(budget, start, horizon_end, share,
                                 course=rotate, min_task=per_task)
    weeks = _weeks_between(horizon_end + timedelta(days=1), exam, budget, share,
                           rotate, per_task) if horizon_end < exam else []

    generated = spaced_review.build_windowed_plan(
        states, caps, weeks, per_task_minutes=per_task)
    _stamp_due(generated["tasks"])

    # LLM 说明仍走原来的 agent，只是把"每天 N 分钟"换成这份预算下的日均值 ——
    # 提示词里那句"每天 X 分钟"若还报用户填的原值，会与实际排出来的量对不上。
    avg = round(sum(caps.values()) / len(caps)) if caps else daily_minutes
    summary = review_planner.summarize(states, days_to_exam=days, daily_minutes=avg,
                                       goal=goal, course=course)

    return db.create_review_plan(
        username, course, goal, start.isoformat(), exam.isoformat(), daily_minutes,
        summary, generated["tasks"], *_baselines(states),
        budget_snapshot=json.dumps(budget, ensure_ascii=False),
        materialized_through=horizon_end.isoformat(),
        milestones=json.dumps(generated["milestones"], ensure_ascii=False))


def _baselines(states: list[dict]) -> tuple[float, int]:
    return (sum(float(r["mastery"] or 0) for r in states) / len(states),
            sum(int(r["error_freq"] or 0) for r in states))


def _stamp_due(tasks: list[dict]) -> None:
    """把排期器给的容差天数换算成 due_to。日期加法留在这一层做，
    spaced_review 因此不需要 import datetime（见该模块的纯函数纪律）。"""
    for t in tasks:
        anchor = _date(t["task_date"])
        t["due_from"] = t.get("due_from") or t["task_date"]
        t["due_to"] = (anchor + timedelta(days=int(t.pop("due_offset_days", 1)))).isoformat()


def get_active(username: str, course: str) -> dict | None:
    reconcile(username, course)
    plan = _decorate(db.get_active_review_plan(username, course), username, course)
    if plan:
        plan["today"] = today_view(plan, username, course)
        plan["week"] = week_view(plan, username, course)
    return plan


def manual_checkin(username: str, task_id: int) -> dict:
    task = db.get_review_task(task_id, username)
    if not task or task["status"] != "active":
        raise ValueError("复习任务不存在或不属于当前用户")
    if int(task.get("target_count") or 0) > 0:
        raise ValueError("练习任务要做题判对才算完成，不能手动打卡")
    db.complete_review_task(task_id)
    return get_active(username, task["course_id"]) or {}


def record_attempt(username: str, task_id: int, knowledge_id: int,
                   correct: bool, hints_used: int = 0, problem_id: int = 0) -> dict | None:
    """判题完成后的内部入口；只接受服务端判题结果，不信任前端自评。"""
    if not task_id:
        return None
    task = db.get_review_task(task_id, username)
    if not task or task["status"] != "active":
        raise ValueError("复习任务不存在或不属于当前用户")
    if int(task["knowledge_id"] or 0) != int(knowledge_id or 0):
        raise ValueError("作答题目与复习任务知识点不一致")
    if task["completed"]:
        return task
    if not db.claim_review_task_problem(task_id, problem_id, correct, hints_used):
        return task
    updated = db.increment_review_task(task_id)
    if updated and updated["completed"]:
        all_correct, max_hints = db.review_task_performance(task_id)
        _advance_and_reschedule(task, all_correct, max_hints)
    return updated


def _advance_and_reschedule(task: dict, correct: bool, hints_used: int) -> None:
    interval, stage = spaced_review.next_interval(int(task["stage"] or 0), correct, hints_used)
    anchor = max(date.today(), _date(task.get("task_date"), date.today()))
    due = anchor + timedelta(days=interval)
    db.set_review_stage(task["username"], int(task["knowledge_id"]), stage, due.isoformat())

    remaining = [t for t in db.list_review_tasks(int(task["plan_id"]))
                 if int(t["knowledge_id"] or 0) == int(task["knowledge_id"])
                 and not t["completed"] and int(t["id"]) != int(task["id"])]
    exam = _date(task["exam_date"])
    all_tasks = db.list_review_tasks(int(task["plan_id"]))
    cap = max(1, int(task.get("daily_minutes") or 60) // 15)
    occupied: dict[str, int] = {}
    moving = {int(t["id"]) for t in remaining}
    for row in all_tasks:
        if int(row["id"]) not in moving:
            occupied[row["task_date"]] = occupied.get(row["task_date"], 0) + 1

    cursor = due
    current_stage = stage
    for row in remaining:
        while cursor <= exam and occupied.get(cursor.isoformat(), 0) >= cap:
            cursor += timedelta(days=1)
        if cursor > exam:
            break
        next_gap, next_stage = spaced_review.next_interval(current_stage, True, 0)
        day_index = (cursor - _date(task["start_date"])).days + 1
        slack = max(1, round(next_gap * spaced_review.DUE_SLACK_RATIO))
        db.update_review_task_schedule(
            int(row["id"]), day_index, cursor.isoformat(), current_stage, next_gap,
            due_from=cursor.isoformat(),
            due_to=(cursor + timedelta(days=slack)).isoformat())
        occupied[cursor.isoformat()] = occupied.get(cursor.isoformat(), 0) + 1
        cursor += timedelta(days=next_gap)
        current_stage = next_stage


def progress(username: str, course: str) -> dict | None:
    plan = get_active(username, course)
    return plan.get("progress") if plan else None


# ── 落后自愈 ────────────────────────────────────────────────────────
#
# 原先落后是没有任何机制介入的：_advance_and_reschedule 只在**做完**一项时触发，
# 而 learning_center 把所有 task_date <= today 的未完成任务都算进"今日待办"。
# 落下三天 = 今天顶着二十几项，然后就再也追不上了。
#
# reconcile 每天跑一次（幂等），做两件事：续排滚动窗口、把逾期的债务摊平或减记。


def reconcile(username: str, course: str, today: date | None = None) -> None:
    """按天幂等的计划自愈。get_active 每次调用它，因此复习页与学习中心都自动受益。"""
    if not username:
        return
    plan = db.get_active_review_plan(username, course)
    if not plan:
        return
    today = today or date.today()
    stamp = today.isoformat()
    if plan.get("reconciled_on") == stamp:
        return
    # 预算路径才有 materialized_through；定额计划保持原样，只做逾期收拢。
    if plan.get("budget_snapshot"):
        _extend_horizon(username, course, plan, today)
        plan = db.get_active_review_plan(username, course) or plan
    _settle_debt(username, course, plan, today)
    db.update_review_plan_scheduling(int(plan["id"]), reconciled_on=stamp)


def _extend_horizon(username: str, course: str, plan: dict, today: date) -> None:
    """滚动续排：把详细排期推到 today + HORIZON_DAYS。

    这让计划长期活着 —— 用户不需要每隔两周手动点一次"重新生成"，
    而每次续排用的都是**当下**的掌握度画像和**当下**的预算，比一次性排到考试更准。
    """
    exam = _date(plan["exam_date"], today)
    through = _date(plan.get("materialized_through") or "", today - timedelta(days=1))
    target = min(exam, today + timedelta(days=study_budget.HORIZON_DAYS - 1))
    window_start = max(through + timedelta(days=1), today)
    if window_start > target:
        return

    states = profile.build_profile(username, course, limit=50)
    if not states:
        return
    # 已有未完成任务的知识点跳过：它已经在计划里了，再排一遍就是同一个点占两个位置。
    busy = {int(t["knowledge_id"] or 0) for t in plan.get("tasks", [])
            if not t["completed"] and not t.get("dropped")}
    budget, share, rotate = _budget_for(username, course, int(plan.get("daily_minutes") or 60))
    per_task = study_budget.per_task_minutes(username)
    caps = study_budget.day_caps(budget, window_start, target, share,
                                 course=rotate, min_task=per_task)
    weeks = _weeks_between(target + timedelta(days=1), exam, budget, share,
                           rotate, per_task) if target < exam else []

    generated = spaced_review.build_windowed_plan(
        states, caps, weeks, per_task_minutes=per_task,
        skip_knowledge_ids=busy)
    _stamp_due(generated["tasks"])
    start = _date(plan["start_date"], today)
    for t in generated["tasks"]:
        t["day"] = (_date(t["task_date"]) - start).days + 1
    db.append_review_tasks(int(plan["id"]), generated["tasks"])
    db.update_review_plan_scheduling(
        int(plan["id"]), materialized_through=target.isoformat(),
        milestones=json.dumps(generated["milestones"], ensure_ascii=False))


def _settle_debt(username: str, course: str, plan: dict, today: date) -> None:
    """把逾期未完成的任务摊到未来，超出偿还能力的部分减记。

    减记规则：`review_round > 1` 的巩固轮次直接放弃（它们是深度，可以牺牲），
    第一轮保留但降级为选做（那是内容本身，不能丢，但也不该继续压在必做位上）。
    不这么分的话，要么债务无限堆积，要么把真正没学过的东西丢掉。
    """
    overdue = [t for t in plan.get("tasks", [])
               if not t["completed"] and not t.get("dropped")
               and (t.get("due_to") or t.get("task_date") or "") < today.isoformat()]
    if not overdue:
        return
    overdue.sort(key=lambda t: (-float(t.get("priority") or 0), int(t.get("review_round") or 1)))

    budget, share, rotate = _budget_for(username, course, int(plan.get("daily_minutes") or 60))
    per_task = study_budget.per_task_minutes(username)
    exam = _date(plan["exam_date"], today)
    start = _date(plan["start_date"], today)

    # 补做槽位：未来 DEBT_HORIZON_DAYS 天，每天至多 MAX_DEBT_PER_DAY 项。
    # 用 catchup_minutes 而不是 day_minutes —— 机动日在这里是有容量的，
    # 它存在的意义就是"落后了拿来补"。
    slots: list[date] = []
    for i in range(DEBT_HORIZON_DAYS):
        d = today + timedelta(days=i)
        if d > exam:
            break
        if study_budget.catchup_minutes(budget, d, share, course=rotate,
                                        min_task=per_task) <= 0:
            continue
        slots.extend([d] * study_budget.MAX_DEBT_PER_DAY)

    for i, task in enumerate(overdue):
        if i < len(slots):
            d = slots[i]
            # due_from 保持原值不动：这条任务**本来**该在那天做，这个事实要留着。
            # 界面据此把它显示成"补做"而不是"今天的新任务"。
            db.reschedule_review_task(
                int(task["id"]), day_index=(d - start).days + 1, task_date=d.isoformat(),
                due_from=task.get("due_from") or task.get("task_date") or d.isoformat(),
                due_to=d.isoformat())
        elif int(task.get("review_round") or 1) > 1:
            db.reschedule_review_task(
                int(task["id"]), day_index=int(task["day_index"] or 0),
                task_date=task.get("task_date") or today.isoformat(),
                due_from=task.get("due_from") or "", due_to=task.get("due_to") or "",
                dropped=True)
        else:
            # 还不上的第一轮：摊到偿还窗口之后，并降级为选做，不再算逾期。
            d = min(exam, today + timedelta(days=DEBT_HORIZON_DAYS + (i % 7)))
            db.reschedule_review_task(
                int(task["id"]), day_index=(d - start).days + 1, task_date=d.isoformat(),
                due_from=task.get("due_from") or task.get("task_date") or d.isoformat(),
                due_to=d.isoformat(), tier="optional")


# ── 今日 / 本周视图 ──────────────────────────────────────────────────

def classify_day(tasks: list[dict], stamp: str, is_buffer_day: bool, *,
                 include_overdue: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
    """把某一天的任务分成 必做 / 选做 / 补做，三类互斥（测试报告 I-04）。

    今日卡片和本周格子都用它 —— 此前两处各分各的：本周格子把挪进来的补做也数成
    "必做"，于是同一天格子上写"必做 2 项"、卡片上写"1 项必做 · 待补 2 项"；
    休息日格子上也会出现"必做 1 项"。

    - 补做：原定日期（due_from）早于这一天、被挪过来的任务，做没做完都算补做；
      `include_overdue` 时（今日视图）再加上还没来得及挪的过期未完成任务。
    - 休息日不派必做：落在休息日的非补做任务一律按选做算（多半是排计划之后才把
      这天设成休息日）。所以休息日只可能有补做和选做。
    - 其余按任务自己的 tier。
    """
    core, optional, catchup = [], [], []
    for t in tasks:
        task_date = t.get("task_date") or ""
        due_from = t.get("due_from") or task_date
        if task_date == stamp:
            if due_from < stamp:
                catchup.append(t)
            elif t.get("tier", "core") == "core" and not is_buffer_day:
                core.append(t)
            else:
                optional.append(t)
        elif include_overdue and task_date < stamp and not t["completed"]:
            catchup.append(t)
    return core, optional, catchup


def today_view(plan: dict | None, username: str, course: str,
               today: date | None = None) -> dict:
    """今天要做什么。`done` 只看必做 —— 做完必做当天就是绿的。

    这是四条留白机制里对体感影响最大的一条：一天 8 项全部同等地位、做完 7 项
    依然是红的，是"死死排满"最伤人的地方，而它跟排了多少项其实没关系。
    """
    today = today or date.today()
    stamp = today.isoformat()
    budget, share, rotate = _budget_for(username, course,
                                        int((plan or {}).get("daily_minutes") or 60))
    empty = {"date": stamp, "budget_minutes": study_budget.raw_day_minutes(budget, today),
             "planned_minutes": 0, "core": [], "optional": [], "catchup": [],
             "done": True, "is_buffer_day": budget["buffer_weekday"] == today.weekday()}
    if not plan:
        return empty

    live = [t for t in plan.get("tasks", []) if not t.get("dropped")]
    core, optional, catchup = classify_day(live, stamp, empty["is_buffer_day"],
                                           include_overdue=True)

    return {**empty,
            "planned_minutes": sum(int(t.get("minutes") or 0) for t in core + optional),
            "core": core, "optional": optional, "catchup": catchup,
            "done": all(t["completed"] for t in core)}


def week_view(plan: dict | None, username: str, course: str,
              today: date | None = None) -> list[dict]:
    """未来七天的负载条：每天的预算 / 已排 / 已完成，以及它是不是机动日。"""
    today = today or date.today()
    budget, share, rotate = _budget_for(username, course,
                                        int((plan or {}).get("daily_minutes") or 60))
    # 画留白要用的基准。budget_minutes 走 raw_day_minutes，是**四门课共用**的原始
    # 分钟数；planned_minutes 却只统计这一门。两者相减出来的空缺里混着"另外三门的
    # 配额"和"系统刻意留白"，前端没法把它们分开画。cap_minutes 给出这门课当天真正
    # 的排期上限（已乘课程占比与填充率），于是：
    #     budget - cap  = 其他科目占用      cap - planned = 刻意留白
    # 用的是排期算法自己在调的那个函数（day_minutes 内部就走它），不另算近似值。
    per_task = study_budget.per_task_minutes(username)
    exceptions = {e["date"] for e in budget.get("exceptions", [])}
    by_date: dict[str, list[dict]] = {}
    for t in (plan or {}).get("tasks", []):
        if not t.get("dropped"):
            by_date.setdefault(t.get("task_date") or "", []).append(t)

    out = []
    for i in range(7):
        d = today + timedelta(days=i)
        stamp = d.isoformat()
        tasks = by_date.get(stamp, [])
        is_buffer = budget["buffer_weekday"] == d.weekday()
        core, optional, catchup = classify_day(tasks, stamp, is_buffer)
        out.append({
            "date": stamp, "weekday": d.weekday(),
            "budget_minutes": study_budget.raw_day_minutes(budget, d),
            "planned_minutes": sum(int(t.get("minutes") or 0) for t in tasks),
            "done_minutes": sum(int(t.get("minutes") or 0) for t in tasks if t["completed"]),
            # 三个计数互斥，与今日卡片同一个分法（classify_day）
            "core_count": len(core),
            "optional_count": len(optional),
            "catchup_count": len(catchup),
            "is_buffer_day": is_buffer,
            "is_exception": stamp in exceptions,
            # 用 catchup_minutes 而不是 day_minutes：休息日在后者返回 0，前端就失去
            # 了刻度基准，而休息日恰恰需要能画出落进来的补做任务。
            "cap_minutes": study_budget.catchup_minutes(
                budget, d, share, course=rotate, min_task=per_task),
        })
    return out
