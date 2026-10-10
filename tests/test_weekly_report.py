"""学情周报测试。

这页报表的风险全在"数字是不是这一周的"上——聚合写错了不会抛异常，只会安静地
把累计量当成周增量报出去，而周报又恰恰是最容易被截图转发的一页。所以用例都压在
边界与口径上：上周的作答不能算进本周、没有基线时掌握度变化必须是 None 而不是 0、
resolved_at 为 NULL 的历史错题不能被算进任意一周。

LLM 全程 monkeypatch，不需要 .env 里的 key。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.agents import weekly_report as wr
from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token

USER = "report_tester"
COURSE = "ds-algo"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 测试使用统一端点桩；隔离部署目录中的视觉/文本专用配置。
    monkeypatch.setattr(settings, "llm_text_base_url", "")
    monkeypatch.setattr(settings, "llm_text_api_key", "")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER, 'student')}"}


@pytest.fixture()
def llm_on(monkeypatch):
    """假装配好了文本模型，点评固定返回一句话。"""
    monkeypatch.setattr(settings, "llm_base_url", "http://stub/v1")
    monkeypatch.setattr(settings, "llm_api_key", "stub-key")
    monkeypatch.setattr(settings, "llm_workhorse_model", "stub-workhorse")
    calls: list[list[dict]] = []

    def _fake(messages, max_tokens=None):
        calls.append(messages)
        return "本周作答稳定，下周把栈与队列补上。"

    monkeypatch.setattr(wr.llm_mod, "ask_workhorse", _fake)
    return calls


def _kid(name: str) -> int:
    return db.get_or_create_knowledge_id(name, COURSE)


def _at(conn_sql: str, params: tuple) -> None:
    with db.get_conn() as conn:
        conn.execute(conn_sql, params)


def _attempt(kid: int, score: float, when: str, duration: int = 60, hints: int = 0) -> None:
    """按指定时刻插一条作答。db.save_attempt 只会写 now()，测边界得自己指定时间。"""
    aid = db.save_attempt(USER, "ans", score, hints_used=hints, duration=duration, knowledge_id=kid)
    _at("UPDATE practice_attempts SET created_at=? WHERE id=?", (when, aid))


def _error(kid: int, code: str, when: str) -> int:
    eid = db.save_error_record(USER, code, "诊断", knowledge_id=kid)
    _at("UPDATE error_records SET created_at=? WHERE id=?", (when, eid))
    return eid


def _snapshot(kid: int, mastery: float, when: str) -> None:
    with db.get_conn() as conn:
        conn.execute("INSERT INTO mastery_snapshots(username,knowledge_id,mastery,state,created_at) "
                     "VALUES(?,?,?,?,?)", (USER, kid, mastery, "learning", when))


_REAL_BOUNDS = wr.week_bounds


def _bounds(offset: int = 0):
    """把"本周"钉死在含 2026-08-26（周三）的那一周，用例里所有时刻都相对它写。
    monkeypatch 到 wr.week_bounds 上，免得用例随真实日期漂移。
    调的是模块导入时抓下的原函数，不然打完桩自己调自己就无限递归了。"""
    from datetime import date
    return _REAL_BOUNDS(offset, date(2026, 8, 26))


# ---------- 周边界 ----------

def test_week_bounds_is_monday_to_next_monday():
    from datetime import date
    # 2026-08-26 是周三
    assert wr.week_bounds(0, date(2026, 8, 26)) == ("2026-08-24", "2026-08-31")
    assert wr.week_bounds(1, date(2026, 8, 26)) == ("2026-08-17", "2026-08-24")
    # 周一当天属于本周，不属于上一周
    assert wr.week_bounds(0, date(2026, 8, 24))[0] == "2026-08-24"


def test_last_week_attempts_excluded_from_this_week(client, monkeypatch):
    kid = _kid("栈和队列")
    _attempt(kid, 100, "2026-08-23 22:00:00")   # 上周日
    _attempt(kid, 100, "2026-08-24 09:00:00")   # 本周一
    _attempt(kid, 40, "2026-08-26 09:00:00")
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0, with_comment=False)
    assert r["activity"]["attempts"] == 2
    assert r["activity"]["correct"] == 1
    assert r["activity"]["active_days"] == 2

    prev = wr.build_report(USER, COURSE, 1, with_comment=False)
    assert prev["activity"]["attempts"] == 1


# ---------- 掌握度基线 ----------

def test_no_baseline_reports_none_not_zero(client, monkeypatch):
    """没有周初快照时 average_delta 必须是 None。0.0 会被读成"这周没涨"。"""
    kid = _kid("查找")
    _attempt(kid, 100, "2026-08-26 09:00:00")
    db.upsert_mastery(USER, kid, 0.6, 0.4, "learning")   # 快照落在本周内，不构成基线
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0, with_comment=False)
    assert r["mastery"]["average_delta"] is None
    assert r["mastery"]["has_baseline"] is False


def test_delta_only_counts_points_present_in_baseline(client, monkeypatch):
    """本周才第一次练的知识点不参与涨幅平均，否则"从无到有"会被算成本周进步。"""
    old, fresh = _kid("排序"), _kid("图")
    _snapshot(old, 0.40, "2026-08-20 10:00:00")          # 周初之前的基线
    db.upsert_mastery(USER, old, 0.60, 0.5, "learning")  # 本周涨到 0.60
    db.upsert_mastery(USER, fresh, 0.90, 0.5, "learning")  # 本周新点，无基线
    _attempt(old, 100, "2026-08-26 09:00:00")
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0, with_comment=False)
    assert r["mastery"]["has_baseline"] is True
    # 只有 old 参与：(0.60-0.40)/1 = 0.20；若把 fresh 按基线 0 算进来会变成 0.55
    assert r["mastery"]["average_delta"] == pytest.approx(0.20, abs=1e-6)
    assert [g["name"] for g in r["mastery"]["gained"]] == ["排序"]


def test_baseline_takes_latest_snapshot_before_window(client, monkeypatch):
    kid = _kid("树与二叉树")
    _snapshot(kid, 0.10, "2026-08-10 10:00:00")
    _snapshot(kid, 0.30, "2026-08-21 10:00:00")   # 更近的一条才是基线
    db.upsert_mastery(USER, kid, 0.50, 0.5, "learning")
    _attempt(kid, 100, "2026-08-26 09:00:00")
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0, with_comment=False)
    assert r["mastery"]["gained"][0]["before"] == pytest.approx(0.30)


# ---------- 错题窗口 ----------

def test_legacy_resolved_errors_without_timestamp_are_not_counted(client, monkeypatch):
    """resolved_at 是后补的列，历史已销账错题为 NULL，不能被算进任何一周。"""
    kid = _kid("线性表")
    old = _error(kid, "calc_error", "2026-07-01 10:00:00")
    _at("UPDATE error_records SET resolved=1 WHERE id=?", (old,))   # 没有 resolved_at
    fresh = _error(kid, "concept_confuse", "2026-08-25 10:00:00")
    _at("UPDATE error_records SET resolved=1, resolved_at=? WHERE id=?",
        ("2026-08-26 11:00:00", fresh))
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0, with_comment=False)
    assert r["errors"]["opened"] == 1      # 只有本周新增的那道
    assert r["errors"]["closed"] == 1      # 只有带时间戳的那道


def test_resolve_error_records_stamps_resolved_at(client):
    kid = _kid("串")
    eid = db.save_error_record(USER, "calc_error", "诊断", knowledge_id=kid, problem_id=7)
    assert db.resolve_error_records(USER, kid, problem_id=7) == 1
    with db.get_conn() as conn:
        row = conn.execute("SELECT resolved, resolved_at FROM error_records WHERE id=?",
                           (eid,)).fetchone()
    assert row["resolved"] == 1 and row["resolved_at"]


def test_error_types_are_named(client, monkeypatch):
    kid = _kid("排序")
    _error(kid, "concept_confuse", "2026-08-25 10:00:00")
    _error(kid, "concept_confuse", "2026-08-26 10:00:00")
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0, with_comment=False)
    top = r["errors"]["error_types"][0]
    assert top["cnt"] == 2 and top["name"] and top["name"] != "concept_confuse"


# ---------- 点评的三种"空" ----------

def test_empty_week_skips_llm(client, llm_on, monkeypatch):
    monkeypatch.setattr(wr, "week_bounds", _bounds)
    r = wr.build_report(USER, COURSE, 0)
    assert r["empty"] is True
    assert r["comment"] == ""
    assert r["comment_skipped"] == "no_activity"
    assert llm_on == []          # 空周一次模型都不该调


def test_comment_generated_when_llm_ready(client, llm_on, monkeypatch):
    kid = _kid("栈和队列")
    _attempt(kid, 100, "2026-08-26 09:00:00")
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0)
    assert r["comment"].startswith("本周作答稳定")
    assert r["comment_skipped"] == ""
    # 没有基线时，必须明确告诉模型不要提"和上周相比"
    facts = llm_on[0][1]["content"]
    assert "没有上周的掌握度基线" in facts


def test_comment_skipped_when_llm_not_configured(client, monkeypatch):
    kid = _kid("栈和队列")
    _attempt(kid, 100, "2026-08-26 09:00:00")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0)
    assert r["comment_skipped"] == "llm_unavailable"
    assert r["activity"]["attempts"] == 1     # 数字照出，不因模型缺席而整页打不开


def test_llm_failure_does_not_break_report(client, llm_on, monkeypatch):
    kid = _kid("栈和队列")
    _attempt(kid, 100, "2026-08-26 09:00:00")

    def _boom(messages, max_tokens=None):
        raise RuntimeError("upstream 502")

    monkeypatch.setattr(wr.llm_mod, "ask_workhorse", _boom)
    monkeypatch.setattr(wr, "week_bounds", _bounds)

    r = wr.build_report(USER, COURSE, 0)
    assert r["comment_skipped"] == "llm_failed"
    assert r["activity"]["attempts"] == 1


# ---------- 端点 ----------

def test_endpoint_requires_login(client):
    assert client.get("/report/weekly").status_code == 401


def test_endpoint_clamps_offset(client, auth):
    r = client.get("/report/weekly?offset=999&with_comment=false", headers=auth)
    assert r.status_code == 200
    assert r.json()["offset"] == 52


def test_endpoint_rejects_unknown_course(client, auth):
    assert client.get("/report/weekly?course=nope", headers=auth).status_code == 400
