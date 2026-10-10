"""时间预算驱动的弹性排期测试。

这个功能的价值全在「它**不**做什么」上：不把每天填满、不把落后的债务全堆到今天、
不对着 90 天后的某一天假装精确。这些性质单看接口返回 200 是完全测不出来的，
所以用例几乎全部压在排期结果的形状上：

1. 容量 —— 周模板、例外日、机动日三者的优先级，以及按 408 分值的课间分摊；
2. 留白 —— 任何一天排进去的分钟数都必须低于预算，必做项数必须有上限；
3. 滚动窗口 —— 只详细排两周，更远的只以周为粒度出现在里程碑里；
4. 落后自愈 —— 债务摊平、单日上限、超限时按轮次减记，且当天重复跑不叠加；
5. 向后兼容 —— use_budget=false 仍然是原来那套定额排期。

全程不碰 LLM：settings.text_llm_ready 在测试环境为假，review_planner.summarize
会直接返回空串。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token
from backend.learning import review_service, spaced_review, study_budget

USER = "budget_tester"
COURSE = "ds-algo"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER, 'student')}"}


def _points(n: int = 12, course: str = COURSE) -> list[int]:
    """建 n 个知识点，全部零基础 —— 冷启动时按大纲章节顺序排，结果可预期。"""
    return [db.upsert_knowledge_point(course, f"第{i}章", f"知识点{i}") for i in range(1, n + 1)]


def _save_budget(client, auth, **over) -> dict:
    body = {"weekly_minutes": [60, 60, 60, 60, 60, 120, 120], "intensity": "medium",
            "buffer_weekday": -1, "exam_date": "", "exceptions": []}
    body.update(over)
    r = client.post("/study/budget", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _make_plan(client, auth, *, days: int = 30, course: str = COURSE, **over) -> dict:
    start = date.today()
    body = {"course": course, "start_date": start.isoformat(),
            "exam_date": (start + timedelta(days=days)).isoformat(),
            "daily_minutes": 60, "max_points": 20}
    body.update(over)
    r = client.post("/review/plan", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 容量：周模板 / 例外日 / 机动日 ----------

def test_zero_weekday_gets_no_tasks(client, auth):
    """周模板里填 0 的那个周几，任何一天都不该排到任务。"""
    _points()
    _save_budget(client, auth, weekly_minutes=[0, 120, 120, 120, 120, 120, 120])
    plan = _make_plan(client, auth)

    for t in plan["tasks"]:
        assert date.fromisoformat(t["task_date"]).weekday() != 0, t


def test_exception_date_overrides_weekly_template(client, auth):
    """例外日覆盖周模板：周模板给了时间，例外日填 0 就是 0。"""
    _points()
    blackout = (date.today() + timedelta(days=3)).isoformat()
    _save_budget(client, auth, weekly_minutes=[180] * 7,
                 exceptions=[{"date": blackout, "minutes": 0, "note": "回家"}])
    plan = _make_plan(client, auth)

    assert all(t["task_date"] != blackout for t in plan["tasks"])
    # 只有那一天被挖空，前后两天照常有任务 —— 否则这条断言可能只是"整体没排上"
    neighbours = {(date.today() + timedelta(days=2)).isoformat(),
                  (date.today() + timedelta(days=4)).isoformat()}
    assert neighbours & {t["task_date"] for t in plan["tasks"]}


def test_buffer_weekday_takes_no_new_tasks(client, auth):
    """机动日不排新任务 —— 没落后的时候它就是真的休息日。"""
    _points()
    _save_budget(client, auth, weekly_minutes=[180] * 7, buffer_weekday=6)
    plan = _make_plan(client, auth)

    assert all(date.fromisoformat(t["task_date"]).weekday() != 6 for t in plan["tasks"])


def test_buffer_day_still_has_catchup_capacity():
    """但补做可以用机动日 —— 这正是它与「预算填 0 的那天」唯一的区别。"""
    budget = study_budget.normalize({"weekly_minutes": json.dumps([120] * 7),
                                     "intensity": "medium", "buffer_weekday": 6})
    sunday = next(date.today() + timedelta(days=i) for i in range(7)
                  if (date.today() + timedelta(days=i)).weekday() == 6)
    assert study_budget.day_minutes(budget, sunday) == 0
    assert study_budget.catchup_minutes(budget, sunday) > 0


def test_course_share_follows_408_score(client, auth):
    """同一份预算下，ds-algo(45 分) 拿到的容量约为 net(25 分) 的 1.8 倍。"""
    assert study_budget.course_share("ds-algo") == pytest.approx(45 / 150)
    assert study_budget.course_share("net") == pytest.approx(25 / 150)

    budget = study_budget.normalize({"weekly_minutes": json.dumps([300] * 7),
                                     "intensity": "medium", "buffer_weekday": -1})
    d = date.today()
    ds = study_budget.day_minutes(budget, d, study_budget.course_share("ds-algo"))
    net = study_budget.day_minutes(budget, d, study_budget.course_share("net"))
    assert ds / net == pytest.approx(45 / 25, rel=0.02)


# ---------- 留白：不排满 ----------

def test_daily_load_stays_under_budget(client, auth):
    """排进去的分钟数必须低于当天预算 —— 留白是这个功能的全部意义。"""
    _points(20)
    _save_budget(client, auth, weekly_minutes=[120] * 7)
    plan = _make_plan(client, auth)

    share = study_budget.course_share(COURSE)
    ceiling = 120 * share * study_budget.FILL_RATIO["medium"]
    by_date: dict[str, int] = {}
    for t in plan["tasks"]:
        by_date[t["task_date"]] = by_date.get(t["task_date"], 0) + int(t["minutes"])
    assert by_date, "应当排出任务"
    for stamp, minutes in by_date.items():
        assert minutes <= ceiling, f"{stamp} 排了 {minutes} 分，超出 {ceiling}"


def test_no_day_with_room_is_left_empty(client, auth):
    """有容量的日子不许排空 —— 「不排满」是留白，「有时间却一项不排」是丢容量。

    必做/选做曾经是把当天预算**切成**两个独立的桶（六四开），于是容量落在
    [一项, 一项 ÷ 0.6) 区间的日子两个桶都装不下一项，整天空着。这个区间恰好覆盖
    工作日的常见预算：90 分钟按 408 分值分摊到数据结构再打冲刺档折扣是 22 分钟，
    装得下一项 15 分钟的任务，切开后却是 core 13 / opt 9，两边都不够。
    """
    _points(24)
    # 周三留 0（本来就该空），其余按工作日 90 / 周末 180，机动日关掉，
    # 让每个工作日的单科容量正好落进那个曾经被跳过的区间。
    _save_budget(client, auth, weekly_minutes=[90, 90, 0, 90, 90, 180, 180],
                 intensity="sprint", buffer_weekday=-1)
    plan = _make_plan(client, auth)

    scheduled = {t["task_date"] for t in plan["tasks"]}
    per_task = study_budget.per_task_minutes(USER)
    empty_but_paid = [d for d in plan["week"]
                      if d["budget_minutes"] > 0 and d["planned_minutes"] == 0
                      and d["date"] not in scheduled
                      and int(d["budget_minutes"] * study_budget.course_share(COURSE)
                              * study_budget.FILL_RATIO["sprint"]) >= per_task]
    assert not empty_but_paid, f"这些天有时间却一项没排：{[d['date'] for d in empty_but_paid]}"


def test_small_budget_rotates_instead_of_shredding(client, auth):
    """填了小预算不该比不填更惨。

    60 分钟/天按 408 分值分摊到数据结构只有 12 分钟，装不下一项任务 —— 纯比例分摊
    下这门课一项也排不出来，而**没填过预算**的人 share 取 1.0，反倒拿到完整计划。
    小预算日改为整天轮值给一门课，如实填写才不会被惩罚。
    """
    _points(24)
    _save_budget(client, auth, weekly_minutes=[60] * 7, buffer_weekday=-1)
    plan = _make_plan(client, auth)
    assert plan["tasks"], "小预算下也该排得出任务"

    # 轮到本门的那些天才有任务，其余交给别的课 —— 不再是"每天各分 12 分钟"。
    owned = {t["task_date"] for t in plan["tasks"]}
    assert all(study_budget.day_owner(date.fromisoformat(d)) == COURSE for d in owned)


def test_core_tasks_are_capped_per_day(client, auth):
    """「今日必做」有项数上限。超过三项就不再是必做，那正是压垮人的东西。"""
    _points(30)
    _save_budget(client, auth, weekly_minutes=[480] * 7, intensity="sprint")
    plan = _make_plan(client, auth)

    per_day: dict[str, int] = {}
    for t in plan["tasks"]:
        if t["tier"] == "core":
            per_day[t["task_date"]] = per_day.get(t["task_date"], 0) + 1
    assert per_day
    assert max(per_day.values()) <= spaced_review.MAX_CORE_PER_DAY


def test_followup_rounds_are_never_core(client, auth):
    """第 2 轮起一律选做：巩固是深度，不该去挤复习的广度。"""
    _points(6)
    _save_budget(client, auth, weekly_minutes=[300] * 7)
    plan = _make_plan(client, auth)

    followups = [t for t in plan["tasks"] if int(t["review_round"]) > 1]
    assert followups, "应当排出多轮复习"
    assert all(t["tier"] == "optional" for t in followups)


def test_tasks_carry_a_due_window(client, auth):
    """到期窗口而非到期日：晚一天做不算逾期。"""
    _points()
    _save_budget(client, auth)
    plan = _make_plan(client, auth)

    assert plan["tasks"]
    for t in plan["tasks"]:
        assert t["due_from"] and t["due_to"]
        assert t["due_from"] <= t["due_to"]
        assert t["due_to"] > t["due_from"], "窗口必须真的有宽度"


# ---------- 滚动窗口与里程碑 ----------

def test_only_horizon_is_materialized(client, auth):
    """90 天的计划只详细排未来两周，其余按周出现在里程碑里。"""
    _points(40)
    _save_budget(client, auth, weekly_minutes=[120] * 7)
    plan = _make_plan(client, auth, days=90)

    horizon_end = date.today() + timedelta(days=study_budget.HORIZON_DAYS - 1)
    assert plan["tasks"]
    assert max(t["task_date"] for t in plan["tasks"]) <= horizon_end.isoformat()
    assert plan["milestones"], "窗口之外应当给出周级配额"
    for m in plan["milestones"]:
        assert m["week_start"] > horizon_end.isoformat()
        assert m["point_count"] > 0 and m["minutes"] > 0


def test_horizon_extends_as_days_pass(client, auth):
    """时间推进后自动续排，用户不需要手动点「重新生成」。"""
    _points(40)
    _save_budget(client, auth, weekly_minutes=[120] * 7)
    _make_plan(client, auth, days=90)

    later = date.today() + timedelta(days=5)
    before = {int(t["id"]): t["task_date"]
              for t in db.get_active_review_plan(USER, COURSE)["tasks"]}
    review_service.reconcile(USER, COURSE, today=later)
    rows = db.get_active_review_plan(USER, COURSE)["tasks"]
    after = {int(t["id"]): t["task_date"] for t in rows}

    assert max(after.values()) > max(before.values()), "窗口应当往前推"
    assert len(after) > len(before), "应当补出新的任务行"
    # 续排只追加。到那天为止还没到期的任务，日期一个都不该动
    # （已逾期的会被同一次 reconcile 里的债务收拢挪走，那是另一条机制）。
    untouched = {i: d for i, d in before.items() if d >= later.isoformat()}
    assert untouched and all(after[i] == d for i, d in untouched.items())


# ---------- 落后自愈 ----------

def _make_overdue(plan_id: int, days_ago: int = 3, limit: int = 6) -> list[dict]:
    """把计划里最早的几项改成三天前逾期，用来构造「落下了」这个状态。

    同时清掉 reconciled_on —— 建计划时已经自愈过一轮（get_active 会调 reconcile），
    不清的话下一次 reconcile 会因为"今天跑过了"直接返回，测的就成了幂等而不是自愈。
    """
    stale = (date.today() - timedelta(days=days_ago)).isoformat()
    rows = db.list_review_tasks(plan_id)[:limit]
    with db.get_conn() as conn:
        for r in rows:
            conn.execute("UPDATE review_tasks SET task_date=?,due_from=?,due_to=? WHERE id=?",
                         (stale, stale, stale, r["id"]))
        conn.execute("UPDATE review_plans SET reconciled_on=NULL WHERE id=?", (plan_id,))
    return rows


def test_debt_is_spread_not_piled_on_today(client, auth):
    """落后的任务被摊到未来几天，而不是全部堆在今天。"""
    _points(20)
    _save_budget(client, auth, weekly_minutes=[180] * 7)
    plan = _make_plan(client, auth)
    ids = {int(r["id"]) for r in _make_overdue(int(plan["id"]))}

    review_service.reconcile(USER, COURSE)
    rows = [t for t in db.list_review_tasks(int(plan["id"])) if int(t["id"]) in ids]

    today = date.today().isoformat()
    assert all(t["task_date"] >= today for t in rows), "不该还留在过去"
    landed: dict[str, int] = {}
    for t in rows:
        if not t["dropped"]:
            landed[t["task_date"]] = landed.get(t["task_date"], 0) + 1
    assert len(landed) > 1, "债务应当摊开，不是全压在一天"
    assert max(landed.values()) <= study_budget.MAX_DEBT_PER_DAY


def test_debt_beyond_capacity_is_written_down(client, auth):
    """还不上的部分按轮次减记：巩固轮次放弃，第一轮降级为选做而不是丢掉。"""
    _points(30)
    _save_budget(client, auth, weekly_minutes=[300] * 7, intensity="sprint")
    plan = _make_plan(client, auth)
    # 逾期项数远超偿还能力（DEBT_HORIZON_DAYS × MAX_DEBT_PER_DAY）
    ids = {int(r["id"]) for r in _make_overdue(int(plan["id"]), limit=40)}
    assert len(ids) > study_budget.MAX_DEBT_PER_DAY * review_service.DEBT_HORIZON_DAYS

    review_service.reconcile(USER, COURSE)
    rows = [t for t in db.list_review_tasks(int(plan["id"])) if int(t["id"]) in ids]

    dropped = [t for t in rows if t["dropped"]]
    assert dropped, "超出偿还能力的部分应当有减记"
    assert all(int(t["review_round"]) > 1 for t in dropped), "第一轮是内容本身，不能丢"
    survivors = [t for t in rows if not t["dropped"] and int(t["review_round"]) == 1]
    assert all(t["task_date"] >= date.today().isoformat() for t in survivors)


def test_dropped_tasks_do_not_hurt_completion_rate(client, auth):
    """被系统主动减记的轮次不进完成率分母 —— 否则等于惩罚减负这条机制本身。"""
    _points(30)
    _save_budget(client, auth, weekly_minutes=[300] * 7, intensity="sprint")
    plan = _make_plan(client, auth)
    _make_overdue(int(plan["id"]), limit=40)
    review_service.reconcile(USER, COURSE)

    live = [t for t in db.list_review_tasks(int(plan["id"])) if not t["dropped"]]
    assert db.review_progress(USER, COURSE)["total_tasks"] == len(live)


def test_reconcile_is_idempotent_within_a_day(client, auth):
    """同一天连跑两次，任务分布不变。"""
    _points(20)
    _save_budget(client, auth, weekly_minutes=[180] * 7)
    plan = _make_plan(client, auth)
    _make_overdue(int(plan["id"]))

    review_service.reconcile(USER, COURSE)
    first = {int(t["id"]): (t["task_date"], t["tier"], t["dropped"])
             for t in db.list_review_tasks(int(plan["id"]))}
    review_service.reconcile(USER, COURSE)
    second = {int(t["id"]): (t["task_date"], t["tier"], t["dropped"])
              for t in db.list_review_tasks(int(plan["id"]))}
    assert first == second


def test_catchup_keeps_its_original_due_from(client, auth):
    """补做保留原本的 due_from —— 界面据此把它显示成「补做」而不是今天的新任务。"""
    _points(20)
    _save_budget(client, auth, weekly_minutes=[180] * 7)
    plan = _make_plan(client, auth)
    stale = (date.today() - timedelta(days=3)).isoformat()
    _make_overdue(int(plan["id"]))
    review_service.reconcile(USER, COURSE)

    fresh = review_service.get_active(USER, COURSE)
    assert fresh["today"]["catchup"], "应当认出补做项"
    assert all(t["due_from"] == stale for t in fresh["today"]["catchup"])


# ---------- 今日视图 ----------

def test_today_done_only_looks_at_core(client, auth):
    """做完必做当天就是绿的，选做不影响 —— 这条对体感的影响最大。"""
    _points(20)
    _save_budget(client, auth, weekly_minutes=[300] * 7)
    plan = _make_plan(client, auth)
    view = review_service.get_active(USER, COURSE)["today"]
    assert view["core"] and view["optional"], "今天应当既有必做也有选做"
    assert view["done"] is False

    for t in view["core"]:
        db.complete_review_task(int(t["id"]))
    after = review_service.get_active(USER, COURSE)["today"]
    assert after["done"] is True
    assert any(not t["completed"] for t in after["optional"]), "选做仍未做完"


def test_learning_center_shows_only_core_and_catchup(client, auth):
    """学习中心的「今日待办」不再是"所有过期未完成"，落后也不会顶出二十几项。"""
    _points(30)
    _save_budget(client, auth, weekly_minutes=[300] * 7, intensity="sprint")
    plan = _make_plan(client, auth)
    _make_overdue(int(plan["id"]), limit=40)

    r = client.get(f"/learning/center?course={COURSE}", headers=auth)
    assert r.status_code == 200, r.text
    due = r.json()["due_tasks"]
    assert due, "该有今日任务"
    today = date.today().isoformat()
    for t in due:
        assert not t["dropped"]
        assert t["tier"] == "core" or t["due_from"] < today   # 必做，或者是补做


def test_week_view_marks_buffer_and_exception_days(client, auth):
    """本周负载条要标出机动日与例外日，用户才看得懂"为什么这天是空的"。"""
    _points()
    blackout = (date.today() + timedelta(days=2)).isoformat()
    _save_budget(client, auth, weekly_minutes=[180] * 7, buffer_weekday=6,
                 exceptions=[{"date": blackout, "minutes": 0, "note": "实习"}])
    _make_plan(client, auth)

    week = review_service.get_active(USER, COURSE)["week"]
    assert len(week) == 7
    assert [d for d in week if d["is_buffer_day"]] == [d for d in week if d["weekday"] == 6]
    exception_day = next(d for d in week if d["date"] == blackout)
    assert exception_day["is_exception"] and exception_day["budget_minutes"] == 0


def test_week_view_exposes_this_course_cap(client, auth):
    """cap_minutes = 这门课当天真正的排期上限，前端靠它把"其他科目占用"和
    "刻意留白"分开画。budget_minutes 是四门课共用的原始分钟，两者必须不同。"""
    _points()
    _save_budget(client, auth, weekly_minutes=[180] * 7, buffer_weekday=6)
    _make_plan(client, auth)

    week = review_service.get_active(USER, COURSE)["week"]
    for d in week:
        assert 0 <= d["cap_minutes"] <= d["budget_minutes"]
        assert d["planned_minutes"] <= d["cap_minutes"]
    # 休息日不排新任务，但仍要有刻度基准 —— 补做任务得画得出来
    rest = next(d for d in week if d["is_buffer_day"])
    assert rest["planned_minutes"] == 0
    assert rest["cap_minutes"] > 0


# ---------- 预算读写 ----------

def test_budget_roundtrip_and_defaults(client, auth):
    r = client.get("/study/budget", headers=auth)
    assert r.status_code == 200
    assert r.json()["is_default"] is True

    saved = _save_budget(client, auth, weekly_minutes=[30, 60, 0, 60, 90, 180, 120],
                         intensity="sprint", buffer_weekday=2, exam_date="2026-12-26")
    assert saved["is_default"] is False
    assert saved["weekly_minutes"] == [30, 60, 0, 60, 90, 180, 120]
    assert saved["weekly_total"] == 540
    assert saved["course_shares"]["ds-algo"] == pytest.approx(0.3)


def test_budget_rejects_bad_input(client, auth):
    r = client.post("/study/budget", json={"weekly_minutes": [60, 60, 60]}, headers=auth)
    assert r.status_code == 400
    r = client.post("/study/budget",
                    json={"weekly_minutes": [60] * 7,
                          "exceptions": [{"date": "12月10日", "minutes": 0}]}, headers=auth)
    assert r.status_code == 400


def test_budget_exceptions_are_replaced_not_merged(client, auth):
    """界面上编辑的是整张表，删掉一个例外日必须真的能删掉。"""
    day = (date.today() + timedelta(days=2)).isoformat()
    _save_budget(client, auth, exceptions=[{"date": day, "minutes": 0, "note": "回家"}])
    after = _save_budget(client, auth, exceptions=[])
    assert after["exceptions"] == []


def test_dirty_budget_row_falls_back_to_defaults():
    """脏值一律回落默认值：一个手滑不该让整个复习计划打不开。"""
    normalized = study_budget.normalize({"weekly_minutes": "not json",
                                         "intensity": "极限", "buffer_weekday": 99})
    assert normalized["weekly_minutes"] == study_budget.DEFAULT_WEEKLY
    assert normalized["intensity"] == study_budget.DEFAULT_INTENSITY
    assert normalized["buffer_weekday"] == -1


# ---------- 单题耗时校准 ----------

def test_per_task_minutes_uses_real_durations(client, auth):
    """做题慢的人不该按 15 分钟一题排 —— 那是系统性高估产能。"""
    assert study_budget.per_task_minutes(USER) == 15          # 无样本时的回落值
    for _ in range(5):
        db.save_attempt(USER, "x", 100.0, duration=1500)      # 25 分钟一题
    assert study_budget.per_task_minutes(USER) == 25


def test_zero_durations_are_not_counted(client, auth):
    """duration=0 是"没记"，不是"零秒答完"；混进中位数会把它直接压到 0。"""
    for _ in range(9):
        db.save_attempt(USER, "x", 100.0, duration=0)
    db.save_attempt(USER, "x", 100.0, duration=1200)
    assert study_budget.per_task_minutes(USER) == 20


# ---------- 向后兼容 ----------

def test_uniform_path_still_packs_every_day(client, auth):
    """use_budget=false 仍然是原来那套定额排期：每天等量、填满到考试日。"""
    _points(20)
    _save_budget(client, auth, weekly_minutes=[0] * 7)   # 预算全 0 也不影响定额路径
    plan = _make_plan(client, auth, days=10, use_budget=False, daily_minutes=60)

    assert plan["tasks"]
    dates = {t["task_date"] for t in plan["tasks"]}
    assert len(dates) > 1
    assert all(t["tier"] == "core" for t in plan["tasks"]), "定额路径没有分层，全部视作必做"
    assert not plan["milestones"]


# ---------- 必做 / 选做 / 补做 互斥（测试报告 I-04） ----------

def test_rest_day_never_shows_core_tasks(client, auth):
    """休息日只可能有补做：排完计划之后才把某天设成休息日，那天的任务也不再算必做。"""
    _points(20)
    _save_budget(client, auth, weekly_minutes=[180] * 7, buffer_weekday=-1)
    _make_plan(client, auth)
    tomorrow = date.today() + timedelta(days=1)
    before = next(d for d in review_service.get_active(USER, COURSE)["week"]
                  if d["date"] == tomorrow.isoformat())
    assert before["core_count"] > 0, "设休息日之前，明天应当有必做"

    _save_budget(client, auth, weekly_minutes=[180] * 7, buffer_weekday=tomorrow.weekday())
    week = review_service.get_active(USER, COURSE)["week"]
    for d in week:
        if d["is_buffer_day"]:
            assert d["core_count"] == 0


def test_catchup_on_rest_day_is_counted_as_catchup(client, auth):
    """落下的任务挪到休息日去补：格子上是"补做"，不是"必做"。"""
    _points(30)
    _save_budget(client, auth, weekly_minutes=[180] * 7,
                 buffer_weekday=(date.today() + timedelta(days=1)).weekday())
    plan = _make_plan(client, auth)
    _make_overdue(int(plan["id"]), limit=12)
    week = review_service.get_active(USER, COURSE)["week"]
    rest = next(d for d in week if d["is_buffer_day"])
    assert rest["core_count"] == 0
    assert rest["catchup_count"] > 0, "补做应当能落到休息日"


def test_week_today_cell_matches_today_card(client, auth):
    """本周格子里的"今天"和下面的今日卡片用同一个分法，数字必须一样。"""
    _points(30)
    _save_budget(client, auth, weekly_minutes=[300] * 7)
    plan = _make_plan(client, auth)
    _make_overdue(int(plan["id"]))
    active = review_service.get_active(USER, COURSE)
    cell, card = active["week"][0], active["today"]
    stamp = date.today().isoformat()
    assert cell["core_count"] == len(card["core"])
    assert cell["optional_count"] == len(card["optional"])
    assert cell["catchup_count"] == len([t for t in card["catchup"] if t["task_date"] == stamp])
    # 三类互斥：同一个任务不会同时出现在两组里
    ids = [t["id"] for g in ("core", "optional", "catchup") for t in card[g]]
    assert len(ids) == len(set(ids))
