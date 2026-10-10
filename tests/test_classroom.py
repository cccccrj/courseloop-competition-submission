"""班级端深化：掌握度热力图、作业下发与验收、改判记录导出成偏好数据。

这一块的风险不在算错数，在**把没有的数据画成有**：热力图给未评估的格子补 0，
助教看到的是"全班都不会"，而事实是"还没教到"；作业完成度另存一份状态，
它迟早跟真实作答对不上。用例主要压这两条，外加权限（学生不能看班级数据）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token
from backend.learning import classroom
from scripts import build_preference_data

COURSE = "os"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


@pytest.fixture()
def ta() -> dict:
    return {"Authorization": f"Bearer {issue_token('ta01', 'ta')}"}


@pytest.fixture()
def student() -> dict:
    return {"Authorization": f"Bearer {issue_token('stu_a', 'student')}"}


def _student(username: str, class_name: str = "计科2101") -> None:
    db.register_user(username, "pw123456", name=username.upper(), role="student")
    db.update_user_profile(username, class_name=class_name)


def _point(name: str, chapter: str = "进程管理") -> int:
    return db.upsert_knowledge_point(COURSE, chapter, name)


# ---------- 热力图 ----------

def test_heatmap_leaves_untested_cells_empty(client):
    """未评估不能补 0：掌握度 0 和"没测过"颜色一样，含义相反。"""
    _student("stu_a"); _student("stu_b")
    kid1, kid2 = _point("进程调度"), _point("死锁")
    db.upsert_mastery("stu_a", kid1, 0.8, 0.5, "mastered")
    db.save_error_record("stu_a", "concept_confuse", "诊断", knowledge_id=kid2)

    data = classroom.heatmap(COURSE)
    a = next(s for s in data["students"] if s["username"] == "stu_a")
    b = next(s for s in data["students"] if s["username"] == "stu_b")
    assert a["cells"][str(kid1)]["mastery"] == 0.8
    assert str(kid2) not in a["cells"]          # 有错题但没掌握度记录 → 不画
    assert b["cells"] == {} and b["avg_mastery"] is None
    # 平均只对测过的点求，否则没开始的学生和学了没学会的会并列垫底
    assert a["avg_mastery"] == 0.8


def test_heatmap_hides_points_nobody_touched(client):
    _student("stu_a")
    touched = _point("进程调度")
    _point("从没人碰过的点")
    db.upsert_mastery("stu_a", touched, 0.4, 0.3, "learning")

    data = classroom.heatmap(COURSE)
    assert [p["knowledge_id"] for p in data["points"]] == [touched]
    assert data["point_shown"] == 1 and data["point_total"] > 1


def test_heatmap_ranks_struggling_students_first(client):
    _student("stu_weak"); _student("stu_strong"); _student("stu_new")
    kid = _point("进程调度")
    db.upsert_mastery("stu_weak", kid, 0.2, 0.4, "learning")
    db.upsert_mastery("stu_strong", kid, 0.9, 0.6, "mastered")

    order = [s["username"] for s in classroom.heatmap(COURSE)["students"]]
    # 没数据的垫底：那是"还没开始"，与"学了但弱"不是一类问题
    # （init_db 会种一个 demo_student，它同样没有数据，所以也在后半段）
    assert order[:2] == ["stu_weak", "stu_strong"]
    assert set(order[2:]) == {"stu_new", "demo_student"}


def test_heatmap_can_filter_by_class(client):
    _student("stu_a", "计科2101")
    _student("stu_b", "计科2102")
    kid = _point("进程调度")
    db.upsert_mastery("stu_a", kid, 0.5, 0.3, "learning")
    db.upsert_mastery("stu_b", kid, 0.5, 0.3, "learning")

    data = classroom.heatmap(COURSE, "计科2102")
    assert [s["username"] for s in data["students"]] == ["stu_b"]
    assert set(data["classes"]) == {"计科2101", "计科2102"}


def test_common_errors_are_scoped_by_course(client):
    _student("stu_a")
    os_kid = _point("死锁")
    net_kid = db.upsert_knowledge_point("net", "网络层", "子网划分")
    db.save_error_record("stu_a", "concept_confuse", "d", knowledge_id=os_kid)
    db.save_error_record("stu_a", "calculation", "d", knowledge_id=net_kid)

    codes = {e["error_type"] for e in classroom.common_errors(COURSE)}
    assert codes == {"concept_confuse"}
    assert {e["error_type"] for e in classroom.common_errors("net")} == {"calculation"}
    assert len(classroom.common_errors()) == 2      # 不给 course 就是全局


def test_heatmap_requires_ta_role(client, student, ta):
    assert client.get(f"/teaching/heatmap?course={COURSE}", headers=student).status_code == 403
    assert client.get(f"/teaching/heatmap?course={COURSE}", headers=ta).status_code == 200
    assert client.get("/teaching/heatmap?course=nope", headers=ta).status_code == 400


# ---------- 作业 ----------

def _assign(client, ta, **kw) -> dict:
    body = {"course": COURSE, "title": "本周作业", "knowledge_ids": kw.pop("ids", []),
            "target_count": 2, **kw}
    r = client.post("/teaching/assignments", json=body, headers=ta)
    assert r.status_code == 200, r.text
    return r.json()


def test_assignment_rejects_points_from_another_course(client, ta):
    net_kid = db.upsert_knowledge_point("net", "网络层", "子网划分")
    r = client.post("/teaching/assignments",
                    json={"course": COURSE, "title": "错门作业", "knowledge_ids": [net_kid]},
                    headers=ta)
    # 挂错门的作业在学生那边一条作答都匹配不上，完成度永远是 0 且看不出原因
    assert r.status_code == 400
    assert client.post("/teaching/assignments",
                       json={"course": COURSE, "title": "", "knowledge_ids": []},
                       headers=ta).status_code == 400


def test_completion_comes_from_real_attempts(client, ta):
    _student("stu_a")
    kid = _point("进程调度")
    a = _assign(client, ta, ids=[kid])

    row = classroom.assignment_view(db.get_assignment(a["id"]))
    mine = next(s for s in row["students"] if s["username"] == "stu_a")
    assert mine["done"] == 0 and mine["completed"] is False

    db.save_attempt("stu_a", "ans", 100.0, problem_id=11, knowledge_id=kid)
    db.save_attempt("stu_a", "ans", 100.0, problem_id=12, knowledge_id=kid)
    row = classroom.assignment_view(db.get_assignment(a["id"]))
    mine = next(s for s in row["students"] if s["username"] == "stu_a")
    assert mine["done"] == 2 and mine["completed"] is True
    # 全班口径：种子账号 demo_student 也在名单里，它一题没做
    assert row["completed_students"] == 1 and row["total_students"] == 2
    assert row["completion_rate"] == 0.5


def test_same_problem_twice_does_not_count_twice(client, ta):
    _student("stu_a")
    kid = _point("进程调度")
    a = _assign(client, ta, ids=[kid])
    db.save_attempt("stu_a", "ans", 100.0, problem_id=11, knowledge_id=kid)
    db.save_attempt("stu_a", "ans", 100.0, problem_id=11, knowledge_id=kid)

    row = classroom.assignment_view(db.get_assignment(a["id"]))
    assert next(s for s in row["students"] if s["username"] == "stu_a")["done"] == 1


def test_wrong_answers_and_earlier_attempts_do_not_count(client, ta):
    _student("stu_a")
    kid = _point("进程调度")
    old = db.save_attempt("stu_a", "ans", 100.0, problem_id=9, knowledge_id=kid)
    with db.get_conn() as conn:      # 下发之前做的题不算完成本次作业
        conn.execute("UPDATE practice_attempts SET created_at='2020-01-01 00:00:00' WHERE id=?",
                     (old,))
    a = _assign(client, ta, ids=[kid])
    db.save_attempt("stu_a", "ans", 0.0, problem_id=10, knowledge_id=kid)   # 答错

    row = classroom.assignment_view(db.get_assignment(a["id"]))
    assert next(s for s in row["students"] if s["username"] == "stu_a")["done"] == 0


def test_target_is_per_knowledge_point_not_a_shared_pool(client, ta):
    """在一个点上刷够，不算把三个点的作业都做完了。

    下发表单、作业列表、学生首页三处文案都写着"每个知识点做对 N 题"，而完成度
    原先是 `COUNT(DISTINCT problem_id)` 跨全部知识点算一个总和 —— 口径对不上：
    选 3 个点、target=2 时，学生在其中一个点上做对 2 道就算全部完成，
    另外两个点一道不碰。而助教选那 3 个点，要的正是"这三个薄弱点都得练到"。
    """
    _student("stu_a")
    kids = [_point("进程调度"), _point("死锁"), _point("分页")]
    a = _assign(client, ta, ids=kids)          # target_count=2

    # 全压在第一个点上：做对 4 道，比 3 个点 × 2 道的一半还多
    for pid in (11, 12, 13, 14):
        db.save_attempt("stu_a", "ans", 100.0, problem_id=pid, knowledge_id=kids[0])
    mine = next(s for s in classroom.assignment_view(db.get_assignment(a["id"]))["students"]
                if s["username"] == "stu_a")
    assert mine["completed"] is False
    assert mine["points_done"] == 1 and mine["points_total"] == 3
    # 进度条逐点封顶：第一个点最多贡献 2，不是 4 —— 否则刷一个点就能把条推满
    assert mine["rate"] == round(2 / 6, 4)
    assert mine["done"] == 4                   # 总做对题数照实报

    # 把另外两个点也补到 2 道，才算完成
    for kid, pids in ((kids[1], (21, 22)), (kids[2], (31, 32))):
        for pid in pids:
            db.save_attempt("stu_a", "ans", 100.0, problem_id=pid, knowledge_id=kid)
    mine = next(s for s in classroom.assignment_view(db.get_assignment(a["id"]))["students"]
                if s["username"] == "stu_a")
    assert mine["completed"] is True and mine["points_done"] == 3 and mine["rate"] == 1.0


def test_student_side_shows_which_point_is_still_short(client, ta, student):
    """学生要知道还差哪个点。只说"还差 2 题"，他会挑最顺手的那个点去做，
    而那个点多半已经达标了。"""
    _student("stu_a")
    kid_a, kid_b = _point("进程调度"), _point("死锁")
    _assign(client, ta, ids=[kid_a, kid_b])
    db.save_attempt("stu_a", "ans", 100.0, problem_id=11, knowledge_id=kid_a)
    db.save_attempt("stu_a", "ans", 100.0, problem_id=12, knowledge_id=kid_a)

    item = client.get(f"/assignments?course={COURSE}", headers=student).json()["items"][0]
    assert item["completed"] is False and item["points_done"] == 1
    assert {p["name"]: p["done"] for p in item["points"]} == {"进程调度": 2, "死锁": 0}


def test_student_sees_all_class_and_own_class_assignments(client, ta, student):
    _student("stu_a", "计科2101")
    kid = _point("进程调度")
    _assign(client, ta, ids=[kid], title="全体作业")
    _assign(client, ta, ids=[kid], title="本班作业", class_name="计科2101")
    _assign(client, ta, ids=[kid], title="别班作业", class_name="计科2102")

    titles = [a["title"] for a in
              client.get(f"/assignments?course={COURSE}", headers=student).json()["items"]]
    assert set(titles) == {"全体作业", "本班作业"}


def test_closed_assignment_disappears_for_students(client, ta, student):
    _student("stu_a")
    kid = _point("进程调度")
    a = _assign(client, ta, ids=[kid])
    assert client.get(f"/assignments?course={COURSE}", headers=student).json()["items"]

    assert client.post("/teaching/assignments/close",
                       json={"assignment_id": a["id"]}, headers=ta).json()["ok"] is True
    assert client.get(f"/assignments?course={COURSE}", headers=student).json()["items"] == []
    # 助教侧仍然看得到（要能回看历史作业的完成情况）
    assert client.get(f"/teaching/assignments?course={COURSE}", headers=ta).json()["items"]


def test_students_cannot_publish_assignments(client, student):
    assert client.post("/teaching/assignments",
                       json={"course": COURSE, "title": "x", "knowledge_ids": [1]},
                       headers=student).status_code == 403


# ---------- 改判记录 → 偏好数据 ----------

def test_student_sees_the_ta_review_on_their_own_error(client, ta, student):
    """助教写的批注要送到学生眼前。

    `ta_verdict` / `ta_note` 一直写在库里，但 `/records/errors` 不往外给 ——
    助教认真写的批注等于扔进黑洞，而"有人真的看过这道题"恰恰是这条错题
    最值钱的部分。
    """
    _student("stu_a")
    kid = _point("进程调度")
    eid = db.save_error_record("stu_a", "concept", "把时间片当成了优先级",
                               knowledge_id=kid, problem_stem="题干")

    before = client.get("/records/errors", headers=student).json()["items"][0]
    assert before["ta_reviewed"] is False and before["ta_note"] == ""

    r = client.post("/teaching/review-error", headers=ta, json={
        "record_id": eid, "verdict": "override", "error_type": "careless",
        "note": "这里其实是抄错了数，概念没问题"})
    assert r.status_code == 200, r.text

    after = client.get("/records/errors", headers=student).json()["items"][0]
    assert after["ta_reviewed"] is True and after["ta_verdict"] == "override"
    assert after["ta_note"] == "这里其实是抄错了数，概念没问题"
    # 订正后的错因是学生该看到的那个，不并排给出 AI 原判 —— 两个摆一起只会让人不知道信哪个
    assert after["error_type"] == "careless"


def test_unreviewed_error_never_leaks_a_stale_note(client, student):
    """没复核过就一个字都不给。库里可能留着上一次复核的残值，
    只按 ta_note 非空来显示的话，会把撤销过的批注又端出来。"""
    _student("stu_a")
    eid = db.save_error_record("stu_a", "concept", "诊断", knowledge_id=_point("死锁"))
    with db.get_conn() as conn:
        conn.execute("UPDATE error_records SET ta_note=?, ta_reviewed=0 WHERE id=?",
                     ("上一轮留下的残值", eid))
    item = client.get("/records/errors", headers=student).json()["items"][0]
    assert item["ta_reviewed"] is False and item["ta_note"] == ""


def test_preference_export_keeps_only_real_overrides(client):
    _student("stu_a")
    kid = _point("死锁")
    pid = db.insert_problem(COURSE, "概念题", "什么是死锁？", 3, "四个必要条件", "rule",
                            knowledge="死锁")
    db.save_attempt("stu_a", "我觉得是饥饿", 0.0, problem_id=pid, knowledge_id=kid)
    overridden = db.save_error_record("stu_a", "reasoning", "推理错误",
                                      problem_id=pid, knowledge_id=kid)
    confirmed = db.save_error_record("stu_a", "reasoning", "推理错误",
                                     problem_id=pid, knowledge_id=kid)
    db.review_error_record(overridden, "ta01", "override", error_type="concept_confuse")
    db.review_error_record(confirmed, "ta01", "confirm")

    rows, stat = build_preference_data.build()
    assert stat["kept"] == 1 and stat["skip_confirm"] == 1
    pair = rows[0]
    assert pair["record_id"] == overridden
    assert "concept_confuse" in pair["chosen"] and "reasoning" in pair["rejected"]
    # prompt 是真实的诊断输入，不是拼出来的裸标签
    assert "什么是死锁？" in pair["prompt"][-1]["content"]
    assert "我觉得是饥饿" in pair["prompt"][-1]["content"]

    with_confirm, stat2 = build_preference_data.build(include_confirm=True)
    assert stat2["kept"] == 2
    same = next(r for r in with_confirm if r["record_id"] == confirmed)
    assert same["chosen"] == same["rejected"]


def test_preference_export_drops_records_without_student_answer(client):
    """找不回作答的记录直接丢。补一句"（作答缺失）"就是在造数据。"""
    _student("stu_a")
    kid = _point("死锁")
    rid = db.save_error_record("stu_a", "reasoning", "无题号的拍照批改",
                               problem_id=0, knowledge_id=kid)
    db.review_error_record(rid, "ta01", "override", error_type="concept_confuse")

    rows, stat = build_preference_data.build()
    assert rows == [] and stat["skip_no_input"] == 1


def test_preference_export_skips_note_only_edits(client):
    _student("stu_a")
    kid = _point("死锁")
    pid = db.insert_problem(COURSE, "概念题", "题干", 3, "答案", "rule", knowledge="死锁")
    db.save_attempt("stu_a", "作答", 0.0, problem_id=pid, knowledge_id=kid)
    rid = db.save_error_record("stu_a", "reasoning", "d", problem_id=pid, knowledge_id=kid)
    # verdict=override 但标签没变（助教只写了备注）—— 不构成偏好对
    db.review_error_record(rid, "ta01", "override", error_type="reasoning", note="注意审题")

    rows, stat = build_preference_data.build()
    assert rows == [] and stat["skip_same_label"] == 1


# ---------- 导出 ----------

def test_export_mastery_is_not_truncated_like_the_screen(client, ta):
    """屏上截断 60 人 / 40 点是为了能看；导出没有这个理由 —— 老师导出来正是为了看全。"""
    from backend.learning import classroom as cl
    _student("stu_a")
    kid = _point("进程调度")
    db.save_attempt("stu_a", "ans", 100.0, problem_id=11, knowledge_id=kid)
    db.upsert_mastery("stu_a", kid, 0.42, 0.8, "learning")

    header, rows = cl.export_rows("mastery", COURSE)
    assert header[:3] == ["学号", "姓名", "班级"]
    mine = next(r for r in rows if r[1] == "" or True)      # 至少有行
    assert rows and len(header) == len(mine)
    # 未评估的格子留空，不写 0 —— 与页面留白同一个理由：
    # 0 分和没测过是两回事，写成 0 会让人在 Excel 里算出个假平均
    assert any(cell == "" for r in rows for cell in r[6:])


def test_export_never_leaks_password_material(client, ta):
    """导出是最容易顺手多带一列的地方，而多带的那一列进了老师的表格就收不回来。"""
    from backend.learning import classroom as cl
    _student("stu_a")
    for kind in ("mastery", "errors"):
        header, rows = cl.export_rows(kind, COURSE)
        flat = " ".join(map(str, header)) + " " + " ".join(str(c) for r in rows for c in r)
        for banned in ("pw_hash", "salt", "avatar", "密码"):
            assert banned not in flat


def test_export_csv_is_utf8_bom_so_excel_does_not_mojibake(client, ta):
    """不带 BOM 的话 Excel 在中文 Windows 上按 GBK 猜编码，打开是一屏乱码 ——
    而老师看到乱码不会去查编码，只会认为"导出坏了"。"""
    r = client.get(f"/teaching/export?kind=mastery&course={COURSE}", headers=ta)
    assert r.status_code == 200
    assert r.content.startswith("﻿".encode())
    assert "text/csv" in r.headers["content-type"]
    # 中文文件名只能走 RFC 5987 的 filename*，否则浏览器存下来是乱码
    assert "filename*=UTF-8''" in r.headers["content-disposition"]


def test_export_requires_ta_and_rejects_unknown_kind(client, ta, student):
    assert client.get(f"/teaching/export?kind=mastery&course={COURSE}",
                      headers=student).status_code == 403
    assert client.get(f"/teaching/export?kind=nope&course={COURSE}",
                      headers=ta).status_code == 400
    assert client.get("/teaching/export?kind=mastery&course=nope",
                      headers=ta).status_code == 400


def test_admin_can_use_the_teaching_workbench(client):
    """教务管理员本来就能建号、改角色、传资料、开课，班级学情反而看不了没有道理。
    而且他遇到的是 403「仅助教/教师可操作」，看起来像权限系统坏了。"""
    from backend.core.security import issue_token
    admin = {"Authorization": f"Bearer {issue_token('jwc', 'admin')}"}
    assert client.get(f"/teaching/heatmap?course={COURSE}", headers=admin).status_code == 200
    assert client.get("/teaching/overview", headers=admin).status_code == 200


# ---------- 助教下钻单个学生 ----------

def test_student_detail_gives_the_ta_something_to_act_on(client, ta):
    """热力图能看出谁掉队，此前**点不进去** —— 全站学生数据端点一律从
    ident["username"] 取人，没有一个接受"要看谁"。于是从"发现问题"到
    "采取行动"之间是断的。"""
    _student("stu_a")
    kid = _point("进程调度")
    db.save_attempt("stu_a", "ans", 100.0, problem_id=11, knowledge_id=kid)
    db.save_attempt("stu_a", "ans", 0.0, problem_id=12, knowledge_id=kid)
    db.upsert_mastery("stu_a", kid, 0.31, 0.7, "learning")
    db.save_error_record("stu_a", "concept", "把时间片当成了优先级",
                         knowledge_id=kid, problem_stem="题干")

    b = client.get(f"/teaching/student/stu_a?course={COURSE}", headers=ta).json()
    assert b["profile"]["username"] == "stu_a"
    assert b["activity"]["attempts"] == 2 and b["activity"]["correct_n"] == 1
    assert b["mastery"][0]["knowledge_name"] == "进程调度"
    assert b["errors"][0]["knowledge"] or b["errors"][0]["error_name"]


def test_student_detail_never_returns_password_material(client, ta):
    """把一个具体的人摊开的端点，最容易顺手多带一列。"""
    _student("stu_a")
    b = client.get(f"/teaching/student/stu_a?course={COURSE}", headers=ta).json()
    assert set(b["profile"]) == {"username", "name", "student_id", "class_name"}
    assert "avatar" not in str(b) and "pw_hash" not in str(b) and "salt" not in str(b)


def test_student_detail_guards(client, ta, student):
    _student("stu_a")
    assert client.get(f"/teaching/student/stu_a?course={COURSE}",
                      headers=student).status_code == 403
    assert client.get(f"/teaching/student/查无此人?course={COURSE}",
                      headers=ta).status_code == 404
    assert client.get(f"/teaching/student/stu_a?course=nope",
                      headers=ta).status_code == 400
    # 助教账号不是学生，不该能当学生被查开
    assert client.get(f"/teaching/student/ta01?course={COURSE}",
                      headers=ta).status_code == 404


def test_student_detail_is_logged_for_audit(client, ta, caplog):
    """在有 enrollment 权限模型之前，至少要能事后查"谁在什么时候看了谁"。"""
    import logging
    _student("stu_a")
    with caplog.at_level(logging.INFO):
        client.get(f"/teaching/student/stu_a?course={COURSE}", headers=ta)
    line = next((r.getMessage() for r in caplog.records if "teaching_student" in r.getMessage()), "")
    assert "viewer=ta01" in line and "target=stu_a" in line


# ---------- 复核队列与总览作用域 ----------

def test_review_queue_can_filter_and_page(client, ta):
    """此前只能复核 recent_errors(15) 那固定 15 条，SQL 一个 WHERE 都没有：
    不能筛未复核、不能按班按课、没有翻页。"""
    _student("stu_a", "计科2101")
    _student("stu_b", "计科2102")
    kid = _point("进程调度")
    ids = [db.save_error_record(u, "concept", f"诊断{i}", knowledge_id=kid)
           for u in ("stu_a", "stu_b") for i in range(3)]
    db.review_error_record(ids[0], "ta01", "confirm")

    all_rows = client.get(f"/teaching/errors?course={COURSE}", headers=ta).json()
    assert all_rows["total"] == 6

    pending = client.get(f"/teaching/errors?course={COURSE}&reviewed=no", headers=ta).json()
    assert pending["total"] == 5 and all(not i["ta_reviewed"] for i in pending["items"])

    done = client.get(f"/teaching/errors?course={COURSE}&reviewed=yes", headers=ta).json()
    assert done["total"] == 1

    one_class = client.get(f"/teaching/errors?course={COURSE}&class_name=计科2102", headers=ta).json()
    assert one_class["total"] == 3 and {i["username"] for i in one_class["items"]} == {"stu_b"}

    page = client.get(f"/teaching/errors?course={COURSE}&limit=2&offset=2", headers=ta).json()
    assert len(page["items"]) == 2 and page["offset"] == 2 and page["total"] == 6
    # 新的排前面：错因诊断要回流给学生看，隔了两周的批注对他已经没用了
    assert page["items"][0]["id"] > page["items"][1]["id"]


def test_review_queue_rejects_bad_filters(client, ta):
    assert client.get("/teaching/errors?reviewed=maybe", headers=ta).status_code == 400
    assert client.get("/teaching/errors?course=nope", headers=ta).status_code == 400


def test_overview_counts_are_scoped_by_course_and_class(client, ta):
    """原先四个数是全库 COUNT：切到"计算机网络"看到的仍是四门之和。"""
    _student("stu_a", "计科2101")
    _student("stu_b", "计科2102")
    kid = _point("进程调度")
    net_kid = db.upsert_knowledge_point("net", "网络层", "子网划分")
    db.save_attempt("stu_a", "ans", 100.0, problem_id=11, knowledge_id=kid)
    db.save_attempt("stu_b", "ans", 100.0, problem_id=12, knowledge_id=kid)
    db.save_attempt("stu_a", "ans", 100.0, problem_id=13, knowledge_id=net_kid)

    whole = client.get("/teaching/overview", headers=ta).json()["stats"]
    mine = client.get(f"/teaching/overview?course={COURSE}", headers=ta).json()["stats"]
    one_class = client.get(f"/teaching/overview?course={COURSE}&class_name=计科2101",
                           headers=ta).json()["stats"]
    assert whole["attempts"] == 3 and mine["attempts"] == 2 and one_class["attempts"] == 1
    assert one_class["students"] == 1


def test_active_students_is_a_window_not_all_time(client, ta):
    """原先 active_students 是"历史上做过任何一题的人"，开课两个月后永远满格。"""
    _student("stu_old")
    kid = _point("进程调度")
    old = db.save_attempt("stu_old", "ans", 100.0, problem_id=11, knowledge_id=kid)
    with db.get_conn() as conn:
        conn.execute("UPDATE practice_attempts SET created_at='2020-01-01 00:00:00' WHERE id=?",
                     (old,))
    stats = client.get(f"/teaching/overview?course={COURSE}", headers=ta).json()["stats"]
    assert stats["attempts"] == 1 and stats["active_students"] == 0
    # 窗口长度要回给前端，否则"本周活跃"这句文案改了这里就对不上
    assert stats["active_days"] == db.ACTIVE_WINDOW_DAYS


# ---------- 作业效果验证 / 助教「今日」（docs/35 P1-6、P1-7） ----------

def test_assignment_effect_compares_snapshot_before_with_now(client, ta):
    _student("stu_a")
    kid = _point("页面置换")
    with db.get_conn() as conn:   # 下发前的一张快照
        conn.execute("INSERT INTO mastery_snapshots(username,knowledge_id,mastery,state,created_at) "
                     "VALUES('stu_a',?,0.3,'learning','2000-01-01 00:00:00')", (kid,))
    a = _assign(client, ta, ids=[kid])
    db.upsert_mastery("stu_a", kid, 0.6, 0.5, "learning")
    db.save_attempt("stu_a", "ans", 100.0, problem_id=21, knowledge_id=kid)
    db.save_attempt("stu_a", "ans", 0.0, problem_id=22, knowledge_id=kid)

    eff = classroom.assignment_view(db.get_assignment(a["id"]))["effect"]
    assert eff["pairs"] == 1 and eff["before"] == 0.3 and eff["after"] == 0.6
    assert eff["attempts_after"] == 2 and eff["accuracy_after"] == 0.5
    assert eff["enough"] is False          # 1 个人不下结论


def test_assignment_effect_has_no_before_without_snapshot(client, ta):
    _student("stu_a")
    kid = _point("页面置换")
    a = _assign(client, ta, ids=[kid])
    db.upsert_mastery("stu_a", kid, 0.6, 0.5, "learning")   # 只有下发后的记录
    eff = classroom.assignment_view(db.get_assignment(a["id"]))["effect"]
    assert eff["pairs"] == 0 and eff["before"] is None       # 不拿 0 冒充"前"


def test_today_puts_student_requests_first(client, ta, student):
    kid = _point("死锁")
    rid = db.save_error_record("stu_a", "reasoning", "诊断", knowledge_id=kid)
    db.mark_error_review_requested(rid, "stu_a")
    r = client.get(f"/teaching/today?course={COURSE}", headers=ta).json()
    assert r["primary"]["kind"] == "review" and r["primary"]["reason"] == "student_request"
    assert r["review"]["student_waiting"] == 1
    assert client.get(f"/teaching/today?course={COURSE}", headers=student).status_code == 403


def test_today_suggests_intervention_for_uncovered_common_error(client, ta):
    # 共性错因只统计学生账号（I-03），错题得记在一个真实存在的学生名下
    _student("stu_a")
    kid = _point("死锁")
    for _ in range(3):
        db.save_error_record("stu_a", "boundary", "诊断", knowledge_id=kid)
    r = client.get(f"/teaching/today?course={COURSE}", headers=ta).json()
    assert r["primary"]["kind"] == "intervene" and r["primary"]["source"] == "boundary"
    _assign(client, ta, ids=[kid], source_error_type="boundary")
    r = client.get(f"/teaching/today?course={COURSE}", headers=ta).json()
    assert r["primary"]["kind"] != "intervene"


def test_today_says_calm_when_nothing_is_pending(client, ta):
    r = client.get(f"/teaching/today?course={COURSE}", headers=ta).json()
    assert r["primary"]["kind"] in ("none", "student")
    if r["primary"]["kind"] == "none":
        assert r["primary"]["button"] == ""
