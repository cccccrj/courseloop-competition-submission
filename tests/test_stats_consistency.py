"""跨页面统计一致性（测试报告 I-03）。

同一个学生、同一门课：学习首页、错题与学习记录、助教的学生详情 / 热力图 / 班级总览，
平均掌握度、作答次数、错题数必须是同一个数。此前三处各算各的：
- 平均掌握度：首页按全部知识点平均（没练过按 0），记录页按练过的点且不算遗忘衰减，
  热力图只对屏幕上那几列求 —— 同一人 5.6% / 56.3% 并存；
- 班级总览出现"0 次作答、4 条错题"：拍照批改收录错题时不记作答；
- 共性错因按全体统计，上面的数字却是按班的。

造一个学生，走题库判题以外的几条写入路径（作答 + 掌握度、错题、拍照收录），
再从每个页面的接口把数读出来比对。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token
from backend.learning import classroom

COURSE = "os"
CLASS = "计科2101"
STU = "consist_stu"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


def _h(user: str, role: str) -> dict:
    return {"Authorization": f"Bearer {issue_token(user, role)}"}


@pytest.fixture()
def seeded(client):
    db.register_user(STU, "pw123456", name="一致性", role="student")
    db.update_user_profile(STU, class_name=CLASS)
    # 别的班的学生和助教自己的错题：都不该混进这个班的数里
    db.register_user("other_stu", "pw123456", name="别班", role="student")
    db.update_user_profile("other_stu", class_name="计科2102")

    kids = [db.upsert_knowledge_point(COURSE, "进程管理", n) for n in ("进程调度", "死锁", "信号量")]
    # 两个点有掌握度评估（练过），第三个点只有一条错题
    for kid, m in ((kids[0], 0.8), (kids[1], 0.3)):
        db.save_attempt(STU, "答", 100.0 if m > 0.5 else 0.0, knowledge_id=kid)
        db.upsert_mastery(STU, kid, m, 0.4, "learning")
    db.save_error_record(STU, "boundary", "诊断", knowledge_id=kids[1])
    db.save_error_record("other_stu", "careless", "诊断", knowledge_id=kids[0])
    db.save_error_record("ta01", "careless", "助教自己试做", knowledge_id=kids[0])
    # 拍照批改收录一条错题：现在会同时记一次作答
    r = client.post("/grade/photo/collect", headers=_h(STU, "student"), json={
        "course": COURSE, "knowledge_point": "信号量", "error_type": "concept_confuse",
        "diagnosis": "第二步错", "problem_stem": "我的草稿"})
    assert r.status_code == 200, r.text
    return kids


def test_average_mastery_is_the_same_everywhere(client, seeded):
    stu, ta = _h(STU, "student"), _h("ta01", "ta")
    center = client.get(f"/learning/center?course={COURSE}", headers=stu).json()["overview"]
    records = client.get(f"/records/mastery?course={COURSE}", headers=stu).json()
    detail = client.get(f"/teaching/student/{STU}?course={COURSE}", headers=ta).json()
    heat = next(s for s in classroom.heatmap(COURSE, CLASS)["students"] if s["username"] == STU)

    avg = center["average_mastery"]
    assert avg is not None and 0.5 < avg < 0.56     # (0.8 + 0.3) / 2，当天练的几乎无衰减
    assert records["summary"]["average"] == avg
    assert detail["mastery_summary"]["average"] == avg
    assert heat["avg_mastery"] == avg
    # 覆盖面也一致：只有一条错题、没有评估的点不算"练过"
    assert center["practiced_points"] == records["summary"]["practiced"] == 2
    assert detail["mastery_summary"]["practiced"] == heat["covered"] == 2
    # 列表里每个点的值也是同一口径（有效掌握度），不是一边原始值一边衰减值
    assert {m["knowledge_id"]: m["mastery"] for m in records["items"]} == \
           {m["knowledge_id"]: m["mastery"] for m in detail["mastery"]}


def test_attempt_and_error_counts_agree(client, seeded):
    stu, ta = _h(STU, "student"), _h("ta01", "ta")
    center = client.get(f"/learning/center?course={COURSE}", headers=stu).json()["overview"]
    errs = client.get(f"/records/errors?course={COURSE}", headers=stu).json()
    detail = client.get(f"/teaching/student/{STU}?course={COURSE}", headers=ta).json()
    overview = client.get(f"/teaching/overview?course={COURSE}&class_name={CLASS}", headers=ta).json()

    # 作答：2 次判题 + 1 次拍照收录
    assert center["attempts"] == detail["activity"]["attempts"] == overview["stats"]["attempts"] == 3
    # 错题：首页与记录页同一个计数
    assert errs["counts"]["total"] == center["errors_total"] == 2
    assert errs["counts"]["open"] == center["open_errors"]
    # 班级总览：只算这个班的学生，且不会再出现"错题比作答多"
    assert overview["stats"]["errors"] == 2
    assert overview["stats"]["errors"] <= overview["stats"]["attempts"]
    # 共性错因与上面的数同一范围：别班学生和助教账号的错题不计入
    common = {c["error_type"]: c["count"] for c in overview["common_errors"]}
    assert "careless" not in common and sum(common.values()) == 2


def test_untouched_student_shows_dash_not_zero(client):
    """一个点都没练过时平均掌握度是"没有"，不是 0%。"""
    db.register_user("fresh", "pw123456", name="新生", role="student")
    db.upsert_knowledge_point(COURSE, "进程管理", "进程调度")
    center = client.get(f"/learning/center?course={COURSE}", headers=_h("fresh", "student")).json()
    assert center["overview"]["average_mastery"] is None
    assert center["overview"]["practiced_points"] == 0
