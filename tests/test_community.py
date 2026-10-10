"""学习态势与社区：周榜、同伴匿名对比、连续学习天数。

这块的风险有两类，用例各压一半：

1. **泄身份**。榜是匿名的，除自己外一个身份字段都不该下发。这条不能靠前端自觉，
   得在响应体里验。
2. **把"没学"写成"垫底"**。一周没练的人不上榜、百分位是 None 而不是 0；
   这两件事在页面上是完全不同的两句话。

另外周窗口必须与周报共用一把尺子，否则学生会拿两个"本周"来问哪个是对的。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.agents import weekly_report as wr
from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token
from backend.learning import community

COURSE = "co"
ME = "stu_me"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(ME, 'student')}"}


_REAL_BOUNDS = wr.week_bounds


def _bounds(offset: int = 0):
    """把"本周"钉死在含 2026-08-26（周三）的那一周，与周报测试同一手法。"""
    from datetime import date
    return _REAL_BOUNDS(offset, date(2026, 8, 26))


@pytest.fixture()
def fixed_week(monkeypatch):
    monkeypatch.setattr(community, "week_bounds", _bounds)


def _student(username: str) -> None:
    db.register_user(username, "pw123456", name=f"姓名{username}", role="student")
    db.update_user_profile(username, student_id=f"2021{username}", class_name="计科2101")


def _kid(name: str) -> int:
    return db.upsert_knowledge_point(COURSE, "指令系统", name)


def _attempt(user: str, kid: int, score: float, when: str, pid: int = 0) -> None:
    aid = db.save_attempt(user, "ans", score, problem_id=pid, knowledge_id=kid)
    with db.get_conn() as conn:
        conn.execute("UPDATE practice_attempts SET created_at=? WHERE id=?", (when, aid))


# ---------- 匿名 ----------

def test_board_never_leaks_other_identities(client, auth, fixed_week):
    for u in (ME, "stu_other"):
        _student(u)
    kid = _kid("寻址方式")
    _attempt(ME, kid, 100, "2026-08-25 10:00:00", pid=1)
    _attempt("stu_other", kid, 100, "2026-08-25 10:00:00", pid=2)
    _attempt("stu_other", kid, 100, "2026-08-25 11:00:00", pid=3)

    body = client.get(f"/community/standings?course={COURSE}", headers=auth).json()
    raw = json.dumps(body, ensure_ascii=False)
    # 别人的用户名、姓名、学号一个都不该出现在响应里
    assert "stu_other" not in raw
    assert "姓名stu_other" not in raw and "2021stu_other" not in raw
    assert [r["alias"] for r in body["board"]] == ["第 1 名", "我"]
    assert [r["is_me"] for r in body["board"]] == [False, True]


# ---------- 排名口径 ----------

def test_rank_by_correct_not_by_attempts(client, auth, fixed_week):
    """按作答数排的话，刷一屏错答案就能冲榜。"""
    for u in (ME, "stu_spam"):
        _student(u)
    kid = _kid("寻址方式")
    for i in range(6):
        _attempt("stu_spam", kid, 0, "2026-08-25 10:00:00", pid=100 + i)   # 全错
    _attempt(ME, kid, 100, "2026-08-25 10:00:00", pid=1)

    body = client.get(f"/community/standings?course={COURSE}", headers=auth).json()
    assert body["me"]["rank"] == 1
    assert body["board"][0]["is_me"] is True
    assert body["board"][1]["correct_n"] == 0 and body["board"][1]["attempts"] == 6


def test_ties_share_a_rank_and_skip_the_next(client, auth, fixed_week):
    for u in (ME, "stu_b", "stu_c"):
        _student(u)
    kid = _kid("寻址方式")
    for u in (ME, "stu_b"):
        _attempt(u, kid, 100, "2026-08-25 10:00:00", pid=1)
    _attempt("stu_c", kid, 0, "2026-08-25 10:00:00", pid=2)

    ranks = [r["rank"] for r in
             client.get(f"/community/standings?course={COURSE}", headers=auth).json()["board"]]
    assert ranks == [1, 1, 3]


# ---------- "没学" ≠ "垫底" ----------

def test_inactive_student_is_absent_not_last(client, auth, fixed_week):
    _student(ME)
    _student("stu_other")
    kid = _kid("寻址方式")
    _attempt("stu_other", kid, 100, "2026-08-25 10:00:00", pid=1)

    body = client.get(f"/community/standings?course={COURSE}", headers=auth).json()
    assert body["active_students"] == 1
    assert len(body["board"]) == 1 and body["board"][0]["is_me"] is False
    # 没上榜：名次与百分位都是 None，而不是最后一名 / 0%
    assert body["me"]["on_board"] is False
    assert body["me"]["rank"] is None and body["me"]["percentile"] is None
    assert body["me"]["attempts"] == 0


def test_last_week_attempts_do_not_count_this_week(client, auth, fixed_week):
    _student(ME)
    kid = _kid("寻址方式")
    _attempt(ME, kid, 100, "2026-08-23 22:00:00", pid=1)     # 上周日
    _attempt(ME, kid, 100, "2026-08-25 10:00:00", pid=2)

    now = client.get(f"/community/standings?course={COURSE}", headers=auth).json()
    prev = client.get(f"/community/standings?course={COURSE}&offset=1", headers=auth).json()
    assert now["me"]["correct_n"] == 1 and prev["me"]["correct_n"] == 1
    # 与周报共用 week_bounds，边界必须一致
    assert (now["week_start"], now["week_end"]) == ("2026-08-24", "2026-08-30")


# ---------- 同伴对比 ----------

def test_peer_medians_exclude_myself(client, auth, fixed_week):
    for u in (ME, "stu_b", "stu_c"):
        _student(u)
    kid = _kid("寻址方式")
    db.upsert_mastery(ME, kid, 0.9, 0.6, "mastered")
    db.upsert_mastery("stu_b", kid, 0.4, 0.4, "learning")
    db.upsert_mastery("stu_c", kid, 0.6, 0.4, "learning")
    for u, n in ((ME, 5), ("stu_b", 1), ("stu_c", 3)):
        for i in range(n):
            _attempt(u, kid, 100, "2026-08-25 10:00:00", pid=200 + i)

    body = client.get(f"/community/standings?course={COURSE}", headers=auth).json()
    assert body["peers"]["learners"] == 2
    assert body["peers"]["median_mastery"] == pytest.approx(0.5)     # (0.4+0.6)/2
    assert body["peers"]["median_correct"] == pytest.approx(2.0)     # (1+3)/2
    assert body["me"]["avg_mastery"] == pytest.approx(0.9)
    # 分母不含自己：3 人里排第 1 = 超过了另外 2 个人 = 100%
    assert body["me"]["percentile"] == 1.0


# ---------- 态势 ----------

def test_trend_fills_missing_days_with_zero(client, auth):
    """折线要连着：缺天会被读成"数据丢了"，而事实是"这几天没学"。"""
    _student(ME)
    kid = _kid("寻址方式")
    db.save_attempt(ME, "ans", 100.0, problem_id=1, knowledge_id=kid)   # 今天

    trend = client.get(f"/community/standings?course={COURSE}", headers=auth).json()["trend"]
    assert len(trend) == community.TREND_DAYS
    assert trend[-1]["attempts"] == 1
    assert all(d["attempts"] == 0 for d in trend[:-1])
    days = [d["day"] for d in trend]
    assert days == sorted(days)


def test_streak_allows_starting_from_yesterday():
    from datetime import date, timedelta
    today = date.today()
    yday = today - timedelta(days=1)
    before = today - timedelta(days=2)
    # 今天还没练：从昨天起算，不清零
    assert community.learning_streak([yday.isoformat(), before.isoformat()]) == 2
    assert community.learning_streak([today.isoformat(), yday.isoformat()]) == 2
    assert community.learning_streak([]) == 0
    # 断了一天就断了
    assert community.learning_streak([before.isoformat()]) == 0


def test_rest_day_does_not_break_streak():
    """休息日是产品主动建议的，不能因为你真休息了就把连续天数清零。"""
    from datetime import date, timedelta
    today = date.today()
    # 造一段：今天往前数，第 2 天前是休息日且没有记录
    rest = today - timedelta(days=2)
    days = [(today - timedelta(days=i)).isoformat() for i in (0, 1, 3, 4)]

    # 不告诉它休息日 —— 老行为，断在缺口处
    assert community.learning_streak(days) == 2
    # 告诉它那天是休息日 —— 跨过去继续数，但休息日本身不计入
    assert community.learning_streak(days, rest.weekday()) == 4


def test_streak_is_the_same_number_as_learning_center(client, auth):
    """中心页与态势页必须给同一个数 —— 两处各算各的就会互相打脸。"""
    _student(ME)
    kid = _kid("寻址方式")
    db.save_attempt(ME, "ans", 100.0, problem_id=1, knowledge_id=kid)

    center = client.get(f"/learning/center?course={COURSE}", headers=auth).json()
    board = client.get(f"/community/standings?course={COURSE}", headers=auth).json()
    assert center["overview"]["streak_days"] == board["me"]["streak_days"] == 1


# ---------- 端点 ----------

def test_percentile_is_none_when_i_am_the_only_active_one(client, auth, fixed_week):
    """没有可比对象时不是 100%，是"算不出来"。"""
    _student(ME)
    _attempt(ME, _kid("寻址方式"), 100, "2026-08-25 10:00:00", pid=1)
    body = client.get(f"/community/standings?course={COURSE}", headers=auth).json()
    assert body["me"]["rank"] == 1 and body["me"]["percentile"] is None


def test_endpoint_guards(client, auth):
    assert client.get("/community/standings").status_code == 401
    assert client.get("/community/standings?course=nope", headers=auth).status_code == 400
    assert client.get(f"/community/standings?course={COURSE}&offset=999",
                      headers=auth).json()["offset"] == 52
