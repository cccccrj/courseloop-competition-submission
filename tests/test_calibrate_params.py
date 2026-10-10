"""参数标定的溯源审计：合成数据必须被认出来并挡在拟合之前。

这是这个脚本存在的主要理由 —— 用 bootstrap_demo 生成的记录去拟合遗忘半衰期，
拟合出来的只是生成公式本身，而结果看起来比手调常数更「有据可依」，更危险。
"""
from __future__ import annotations

import json

from backend.core import db
from scripts.calibrate_params import (audit_attempts, audit_snapshots, calibrate_from_logs,
                                      render_markdown)


def _seed(tmp_path, monkeypatch, answers, snapshots):
    from backend.core.config import settings
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "c.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    with db.get_conn() as conn:
        for user, ans, score in answers:
            conn.execute("INSERT INTO practice_attempts(username,problem_id,answer,score,hints_used,"
                         "duration,created_at,knowledge_id,session_id) VALUES(?,1,?,?,0,10,?,1,0)",
                         (user, ans, score, "2026-08-01 10:00:00"))
        for user, value, ts in snapshots:
            conn.execute("INSERT INTO mastery_snapshots(username,knowledge_id,mastery,state,created_at)"
                         " VALUES(?,1,?,'learning',?)", (user, value, ts))
    return db.get_conn


def test_synthetic_attempts_are_flagged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch,
          [("showcase_s01", "演示作答", 100.0), ("showcase_s02", "演示作答", 0.0)], [])
    with db.get_conn() as conn:
        result = audit_attempts(conn)
    assert result["verdict"] == "synthetic"
    assert result["binary_score"] is True


def test_real_looking_attempts_are_not_flagged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch,
          [("liuxx", "我的解答是 O(n log n)", 73.5), ("zhaoyy", "答案 B", 40.0)], [])
    with db.get_conn() as conn:
        result = audit_attempts(conn)
    assert result["verdict"] == "mixed-or-real"


def test_monotonic_snapshots_have_no_forgetting_signal(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [],
          [("a", 0.2, "2026-08-01 20:00:00"), ("a", 0.4, "2026-08-08 20:00:00"),
           ("a", 0.6, "2026-08-15 20:00:00")])
    with db.get_conn() as conn:
        result = audit_snapshots(conn)
    assert result["verdict"] == "monotonic-synthetic"
    assert result["decreasing"] == 0


def test_declining_snapshots_are_usable(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [],
          [("a", 0.6, "2026-08-01 20:00:00"), ("a", 0.5, "2026-08-08 20:00:00")])
    with db.get_conn() as conn:
        result = audit_snapshots(conn)
    assert result["verdict"] == "has-forgetting-signal"
    assert result["decreasing"] == 1


def test_log_stats_and_missing_file(tmp_path):
    assert calibrate_from_logs(str(tmp_path / "nope.jsonl"))["available"] is False
    p = tmp_path / "llm_calls.jsonl"
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"intent": "solve", "model": "m", "verification": {"method": "numeric", "passed": True,
                                                           "confidence": 1.0}, "revised": False},
        {"intent": "solve", "model": "m", "verification": {"method": "numeric", "passed": False,
                                                           "confidence": 1.0}, "revised": True},
        {"intent": "diagnose", "model": "m", "verification": {"method": "unit_test", "passed": False,
                                                              "extra": {"passed": 3, "total": 6}}},
    ]) + "\n不是 json\n", encoding="utf-8")
    out = calibrate_from_logs(str(p))
    assert out["total"] == 3 and out["broken_lines"] == 1
    assert out["intents"]["solve"]["pass_rate"] == 0.5
    assert out["intents"]["solve"]["revise_rate"] == 0.5
    assert out["intents"]["diagnose"]["unit_test_partial"]["median"] == 0.5


def test_markdown_report_states_blockers(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [("showcase_s01", "演示作答", 100.0)],
          [("a", 0.2, "2026-08-01 20:00:00"), ("a", 0.4, "2026-08-08 20:00:00")])
    from scripts.calibrate_params import calibrate
    from backend.core.config import settings
    result = calibrate(settings.db_path, str(tmp_path / "none.jsonl"))
    assert result["blocked"], "合成数据必须产出明确的阻塞说明"
    text = render_markdown(result)
    assert "标不了的部分" in text and "FORGET_HALF_LIFE_DAYS" in text
