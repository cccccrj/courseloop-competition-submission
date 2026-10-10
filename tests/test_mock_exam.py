"""真题模考闭环。

模考的风险集中在**分数**上：一张卷子的总分、每题的权重、部分给分的折算，
错一处考生看到的就是一个假分数，而这个分数还会被截图发出去。所以用例压在：

1. 试卷结构 —— 单选 40 题 80 分 + 综合 7 题 70 分，四门按分值分配，和必须是 150；
2. 组卷 —— 单选按 canonical 的 kind 认，不按 problems.type 那个自由文本列；
   组不满时如实报缺口，不重复出题、不把丢掉的分摊到别的题上；
3. 阅卷 —— 客观题 0/1，综合题按得分点分值折算；**空题不判**（没作答不是做错）；
4. 交卷 —— 只能交一次，成绩单存下来、刷新还在。

LLM 只在 rule 题那条路上出现，用 monkeypatch 挡掉；其余全走确定性判分。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.api import core_routes
from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.courses import all_courses
from backend.core.security import issue_token
from backend.learning import mock_exam

USER = "exam_tester"
OTHER = "exam_intruder"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER, 'student')}"}


@pytest.fixture()
def no_llm(monkeypatch):
    """rule 题必然会调一次诊断补讲解。挡掉它，得分只由得分点覆盖决定。"""
    monkeypatch.setattr(core_routes.diag_mod, "diagnose",
                        lambda *a, **k: {"correct": False, "diagnosis": "打桩诊断",
                                         "error_tags": ["incomplete"], "error_tag_names": ["表达不完整"],
                                         "remediation": ""})


def _choice(course: str, chapter: str, knowledge: str, value: str = "B", n: int = 1) -> list[int]:
    kid = db.upsert_knowledge_point(course, chapter, knowledge)
    out = []
    for i in range(n):
        pid = db.insert_problem(course, "单项选择", f"[{knowledge}] 选择题 {i}", 2,
                                json.dumps({"kind": "choice", "value": value}), "numeric",
                                knowledge=knowledge)
        db.link_problem_knowledge(pid, kid)
        out.append(pid)
    return out


def _numeric(course: str, chapter: str, knowledge: str, value: int = 12) -> int:
    """非选择的数值题 —— 组卷时算综合应用题。"""
    kid = db.upsert_knowledge_point(course, chapter, knowledge)
    pid = db.insert_problem(course, "计算题", f"[{knowledge}] 算一个数", 4,
                            json.dumps({"kind": "int", "value": value}), "numeric",
                            knowledge=knowledge)
    db.link_problem_knowledge(pid, kid)
    return pid


def _rule(course: str, chapter: str, knowledge: str, points: list[tuple[str, float]]) -> int:
    kid = db.upsert_knowledge_point(course, chapter, knowledge)
    pid = db.insert_problem(course, "分析题", f"[{knowledge}] 简述", 4, "参考答案", "rule",
                            knowledge=knowledge)
    db.link_problem_knowledge(pid, kid)
    for desc, score in points:
        db.add_rubric_item(pid, desc, score, expected_evidence=desc)
    return pid


def _full_bank() -> None:
    """四门都给一点题，够组出一张小卷子。"""
    for course, chapters in (("ds-algo", ["绪论", "线性表"]), ("co", ["指令系统"]),
                             ("os", ["进程管理"]), ("net", ["网络层"])):
        for ch in chapters:
            _choice(course, ch, f"{course}-{ch}-选择", n=3)
        _numeric(course, chapters[0], f"{course}-综合")


# ---------- 试卷结构 ----------

def test_apportion_sums_exactly():
    assert sum(mock_exam._apportion(40, [45, 45, 35, 25])) == 40
    assert sum(mock_exam._apportion(70, [45, 45, 35, 25])) == 70
    assert mock_exam._apportion(21, [1, 1]) == [11, 10]      # 分值是整数，不给 10.5
    assert mock_exam._apportion(0, [1, 2]) == [0, 0]


def test_blueprint_matches_408_paper():
    bp = mock_exam.blueprint()
    assert sum(b["choice_count"] for b in bp) == mock_exam.CHOICE_COUNT
    assert sum(b["comprehensive_score"] for b in bp) == mock_exam.COMPREHENSIVE_TOTAL
    assert sum(b["comprehensive_count"] for b in bp) == 7        # 与真卷一致
    assert mock_exam.standard_score() == 150
    # 每门综合题的分值之和必须等于该门的综合分
    for b in bp:
        assert sum(b["comprehensive_item_scores"]) == b["comprehensive_score"]
    # 分值大的门题多，且顺序跟课程注册表一致
    assert [b["choice_count"] for b in bp] == [12, 12, 9, 7]
    # 每门的卷面分必须正好是大纲里的 45/45/35/25。综合分若也按权重单独分一次，
    # 单选取整的零头就没人认领，os 会变成 34 分、net 变成 26 分。
    for b in bp:
        assert b["choice_count"] * b["choice_score"] + b["comprehensive_score"] \
            == all_courses()[b["course"]].score


# ---------- 组卷 ----------

def test_choice_section_is_decided_by_canonical_not_type_column(client):
    """problems.type 是出题时随手写的中文，按它分段迟早会漏。"""
    kid = db.upsert_knowledge_point("net", "网络层", "子网划分")
    # type 写着"单项选择"，canonical 却是 int —— 这是综合题
    liar = db.insert_problem("net", "单项选择", "看着像选择题", 3,
                             json.dumps({"kind": "int", "value": 62}), "numeric",
                             knowledge="子网划分")
    db.link_problem_knowledge(liar, kid)
    real = _choice("net", "网络层", "子网划分")[0]

    paper = mock_exam.build_paper(USER)
    section = {it["problem_id"]: it["section"] for it in paper["items"]}
    assert section[real] == "choice"
    assert section[liar] == "comprehensive"


def test_shortfall_is_reported_and_full_score_is_actual(client):
    _choice("net", "网络层", "子网划分", n=2)
    paper = mock_exam.build_paper(USER)
    assert paper["full_score"] == 2 * mock_exam.CHOICE_SCORE
    assert paper["standard_score"] == 150
    gaps = {(g["course"], g["section"]): (g["got"], g["want"]) for g in paper["shortfall"]}
    assert gaps[("net", "choice")] == (2, 7)
    assert gaps[("ds-algo", "choice")] == (0, 12)
    assert paper["ready"] is False          # 两道题不成一张卷


def test_no_duplicate_problems_and_chapters_rotate(client):
    _choice("os", "进程管理", "进程调度", n=4)
    _choice("os", "内存管理", "分页", n=4)
    paper = mock_exam.build_paper(USER)
    os_items = [it for it in paper["items"] if it["course"] == "os"]
    ids = [it["problem_id"] for it in os_items]
    assert len(ids) == len(set(ids)) == 8      # 出不满 9 道就是 8 道，不重复凑
    assert {it["chapter"] for it in os_items} == {"进程管理", "内存管理"}
    # 轮转：前两道分属两章
    assert os_items[0]["chapter"] != os_items[1]["chapter"]


def test_preview_does_not_leak_stems(client, auth):
    _full_bank()
    r = client.get("/exam/preview", headers=auth).json()
    assert "items" not in r
    assert r["ready"] is True and r["full_score"] > 0
    assert json.dumps(r, ensure_ascii=False).count("选择题 0") == 0


# ---------- 会话 ----------

def test_start_then_resume_same_paper(client, auth):
    _full_bank()
    first = client.post("/exam/start", headers=auth).json()
    again = client.post("/exam/start", headers=auth).json()
    assert again["session_id"] == first["session_id"]
    assert [i["problem_id"] for i in again["items"]] == [i["problem_id"] for i in first["items"]]
    assert again["remaining_seconds"] <= mock_exam.DURATION_MINUTES * 60


def test_start_refuses_when_bank_cannot_make_a_paper(client, auth):
    _choice("net", "网络层", "子网划分", n=2)
    r = client.post("/exam/start", headers=auth)
    assert r.status_code == 503 and "题库" in r.json()["detail"]


def test_answers_are_saved_and_last_write_wins(client, auth):
    _full_bank()
    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    client.post("/exam/save", json={"session_id": sid, "answers": {"1": "A", "2": "B"}}, headers=auth)
    client.post("/exam/save", json={"session_id": sid, "answers": {"1": "C"}}, headers=auth)
    state = mock_exam.session_state(USER, sid)
    assert state["answers"]["1"] == "C" and state["answers"]["2"] == "B"


def test_other_user_cannot_touch_my_exam(client, auth):
    _full_bank()
    sid = client.post("/exam/start", headers=auth).json()["session_id"]
    intruder = {"Authorization": f"Bearer {issue_token(OTHER, 'student')}"}
    assert client.post("/exam/save", json={"session_id": sid, "answers": {"1": "A"}},
                       headers=intruder).status_code == 404
    assert client.get(f"/exam/result/{sid}", headers=intruder).status_code == 404


# ---------- 阅卷 ----------

def _events(client, auth, sid: int) -> list[dict]:
    with client.stream("POST", "/exam/submit/stream", json={"session_id": sid}, headers=auth) as r:
        assert r.status_code == 200
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def _submit(client, auth, sid: int) -> dict:
    return next(e["report"] for e in _events(client, auth, sid) if e["type"] == "report")


def _crash_after(monkeypatch, n: int):
    """批到第 n 道就让阅卷这条生成器死掉 —— 后端重启、连接被掐的等价物。

    真实断线在 TestClient 里模拟不出来：它会把生成器一路跑完，`save_graded`
    照样执行，什么都不会丢。而生产里那条生成器是随连接/进程走的，
    断在哪一道，后面的就都没发生过。

    返回"止血"的那个开关，而不是靠 `monkeypatch.undo()` —— undo 会把 client
    fixture 设的 db_path 一并还原，接下来的请求打到真库上，人都不在，直接 401。
    """
    real = core_routes._grade_one
    state = {"n": 0, "limit": n}

    def spy(*args, **kwargs):
        state["n"] += 1
        if state["n"] > state["limit"]:
            raise RuntimeError("阅卷中途断了")
        return real(*args, **kwargs)

    monkeypatch.setattr(core_routes, "_grade_one", spy)

    def heal() -> None:
        state["limit"] = float("inf")
    return heal


def test_objective_items_are_all_or_nothing(client, auth):
    _full_bank()
    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    choices = [it for it in s["items"] if it["section"] == "choice"]
    answers = {str(it["seq"]): ("B" if i % 2 == 0 else "A") for i, it in enumerate(choices)}
    client.post("/exam/save", json={"session_id": sid, "answers": answers}, headers=auth)

    report = _submit(client, auth, sid)
    right = sum(1 for i, _ in enumerate(choices) if i % 2 == 0)
    assert report["sections"]["choice"]["score"] == right * mock_exam.CHOICE_SCORE
    assert report["correct"] == right


def test_comprehensive_item_gets_partial_credit_by_rubric(client, auth, no_llm):
    """综合题按命中得分点的**分值**占比给分，不是按条数。"""
    _choice("ds-algo", "绪论", "复杂度", n=12)
    _rule("ds-algo", "线性表", "链表", [("头插法", 8.0), ("边界判空", 2.0)])
    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    comp = next(it for it in s["items"] if it["section"] == "comprehensive")
    client.post("/exam/save", headers=auth,
                json={"session_id": sid, "answers": {str(comp["seq"]): "我用头插法建表"}})

    report = _submit(client, auth, sid)
    got = next(i for i in client.get(f"/exam/result/{sid}", headers=auth).json()["items"]
               if i["seq"] == comp["seq"])
    assert got["ratio"] == pytest.approx(0.8)                      # 8/(8+2)
    assert got["score"] == pytest.approx(round(comp["score"] * 0.8, 1))
    assert got["passed"] is False                                  # 缺了得分点，不算全对
    assert report["score"] == pytest.approx(got["score"])


def test_rubric_ratio_weighs_by_score_not_by_count():
    rubric = [{"description": "甲", "score": 9.0}, {"description": "乙", "score": 1.0}]
    assert core_routes._rubric_ratio(rubric, ["甲"]) == pytest.approx(0.9)
    assert core_routes._rubric_ratio(rubric, ["乙"]) == pytest.approx(0.1)
    assert core_routes._rubric_ratio([], ["甲"]) is None            # 没有得分点只能全或无


def test_blank_answers_are_not_graded_as_wrong(client, auth):
    """没作答和做错不是一回事：交白卷不该把错题本灌满。"""
    _full_bank()
    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    report = _submit(client, auth, sid)         # 一题不答直接交

    assert report["score"] == 0 and report["answered"] == 0
    assert db.list_error_records(USER) == []
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM practice_attempts WHERE username=?",
                            (USER,)).fetchone()[0] == 0
    review = client.get(f"/exam/result/{sid}", headers=auth).json()["items"]
    assert all(i["skipped"] for i in review)
    # 没作答也要能看到正确答案，那才是模考的用处；而且参考答案是可读写法，不是 canonical JSON
    assert all(i["reference"] for i in review)
    assert "kind" not in json.dumps([i["reference"] for i in review])


def test_wrong_answers_write_back_to_error_book(client, auth):
    _full_bank()
    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    choices = [it for it in s["items"] if it["section"] == "choice"][:3]
    client.post("/exam/save", headers=auth, json={
        "session_id": sid, "answers": {str(it["seq"]): "A" for it in choices}})
    _submit(client, auth, sid)

    records = db.list_error_records(USER)
    assert len(records) == 3
    assert all(r["knowledge_id"] for r in records)
    assert db.list_mastery(USER)                # 掌握度也跟着更新了


def test_submit_only_once_and_report_survives_reload(client, auth):
    _full_bank()
    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    seq = s["items"][0]["seq"]
    client.post("/exam/save", json={"session_id": sid, "answers": {str(seq): "B"}}, headers=auth)
    report = _submit(client, auth, sid)
    errors_after_first = len(db.list_error_records(USER))

    # 再交一次不报错，直接把存下来的成绩单还回来（标 resumed）。返 409 的话，
    # "交卷后断线重进"这一种就没有出口了 —— 两者在服务端是同一个状态。
    events = _events(client, auth, sid)
    assert [e for e in events if e["type"] == "item"] == []      # 一道题都没重批
    resumed = next(e for e in events if e["type"] == "report")
    assert resumed["resumed"] is True
    assert resumed["report"]["score"] == report["score"]
    assert len(db.list_error_records(USER)) == errors_after_first   # 错题本没被灌第二遍

    stored = client.get(f"/exam/result/{sid}", headers=auth).json()
    assert stored["submitted"] is True
    assert stored["report"]["score"] == report["score"]
    # 交完卷之后不能再存答案
    assert client.post("/exam/save", json={"session_id": sid, "answers": {"1": "D"}},
                       headers=auth).status_code == 404


def test_report_scales_to_150_and_says_so(client, auth):
    """题库不满时满分不是 150。折算值要给，但必须与实际满分同时出现。"""
    _choice("net", "网络层", "子网划分", n=10)      # 多于 net 需要的 7 道，只取 7
    _choice("ds-algo", "绪论", "复杂度", n=4)       # 少于 ds 需要的 12 道，只有 4
    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    client.post("/exam/save", headers=auth, json={
        "session_id": sid, "answers": {str(it["seq"]): "B" for it in s["items"]}})
    report = _submit(client, auth, sid)

    assert report["count"] == 7 + 4
    assert report["full_score"] == 11 * mock_exam.CHOICE_SCORE
    assert report["standard_score"] == 150
    assert report["rate"] == 1.0
    assert report["scaled_score"] == 150.0


def test_weak_chapters_are_ranked_by_rate(client, auth):
    _choice("os", "进程管理", "进程调度", value="B", n=2)
    _choice("os", "内存管理", "分页", value="B", n=2)
    _choice("ds-algo", "绪论", "复杂度", value="B", n=4)
    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    # 进程管理全错、其余全对
    answers = {}
    for it in s["items"]:
        answers[str(it["seq"])] = "A" if it["chapter"] == "进程管理" else "B"
    client.post("/exam/save", json={"session_id": sid, "answers": answers}, headers=auth)
    report = _submit(client, auth, sid)

    assert report["weak_chapters"][0]["chapter"] == "进程管理"
    assert report["weak_chapters"][0]["rate"] == 0.0
    by_course = {c["course"]: c for c in report["courses"]}
    assert by_course["ds-algo"]["rate"] == 1.0


# ---------- 真题优先 ----------

def test_past_paper_preferred_when_both_unseen(client):
    """页面上写着"真题模考"，同样没做过时就该先出真题。

    排序是 (做过没 → 是不是真题 → id)：一道**做过**的真题测的是记忆，
    不如一道没做过的合成题有信息量，所以"没做过"仍排在"是真题"前面。
    """
    kid = db.upsert_knowledge_point("co", "第二章", "补码")
    synth = db.insert_problem("co", "单项选择", "合成的选择题", 2,
                              json.dumps({"kind": "choice", "value": "A"}), "numeric",
                              knowledge="补码", source="seed-practice")
    real = db.insert_problem("co", "单项选择", "真题的选择题", 2,
                             json.dumps({"kind": "choice", "value": "B"}), "numeric",
                             knowledge="补码", source="past-paper")
    for pid in (synth, real):
        db.link_problem_knowledge(pid, kid)

    pool = db.bank_pool(USER, "co")
    picked = mock_exam._select([p for p in pool if mock_exam.is_choice(p)], 1)
    assert [p["problem_id"] for p in picked] == [real], "同样没做过时真题排前面"

    # 做过的真题让位给没做过的合成题
    with db.get_conn() as conn:
        conn.execute("INSERT INTO practice_attempts(username,problem_id,answer,score,hints_used,"
                     "duration,created_at,knowledge_id,session_id) "
                     "VALUES(?,?,'x',100.0,0,10,'2026-08-01 10:00:00',?,0)", (USER, real, kid))
    pool2 = db.bank_pool(USER, "co")
    picked2 = mock_exam._select([p for p in pool2 if mock_exam.is_choice(p)], 1)
    assert [p["problem_id"] for p in picked2] == [synth], "做过的真题测的是记忆，该让位"


def test_paper_reports_past_paper_count(client):
    kid = db.upsert_knowledge_point("co", "第二章", "补码")
    for i, src in enumerate(("past-paper", "past-paper", "seed-practice")):
        pid = db.insert_problem("co", "单项选择", f"题 {i}", 2,
                                json.dumps({"kind": "choice", "value": "B"}), "numeric",
                                knowledge="补码", source=src)
        db.link_problem_knowledge(pid, kid)
    paper = mock_exam.build_paper(USER)
    assert paper["past_paper_count"] == 2
    assert sum(1 for it in paper["items"] if it["is_past_paper"]) == 2


# ---------- 阅卷断了之后 ----------

def test_grading_interrupted_can_be_resumed_and_scores_are_whole(client, auth, monkeypatch):
    """交卷后阅卷断在半路：那场不能就此作废，重发一次交卷要接着批完。

    这是模考里最贵的一次中断 —— 交卷闸已经落下，作答再也改不了，而分数还没算出来。
    早先重发交卷一律 409，于是这场模考只剩一份 0 分成绩单（`score_paper` 对空的
    批改结果求和就是 0），偏偏错题本里已经躺着批过那几道题的记录。
    """
    _full_bank()
    s = client.post("/exam/start", headers=auth).json()
    sid, items = s["session_id"], s["items"]
    client.post("/exam/save", headers=auth, json={
        "session_id": sid, "answers": {str(it["seq"]): "B" for it in items}})

    heal = _crash_after(monkeypatch, 3)
    with pytest.raises(RuntimeError):
        _events(client, auth, sid)

    # 断在这里：卷交了，批了 3 道，成绩单还不存在
    assert db.get_session(sid)["completed_at"]
    assert len(mock_exam.graded_rows(sid)) == 3
    assert mock_exam.stored_report(sid) is None

    # 模考页开局能看见这场没阅完，且给得出 session_id
    prev = client.get("/exam/preview", headers=auth).json()
    assert prev["pending"] is None                       # 不是"还在考"
    assert prev["grading_pending"]["session_id"] == sid
    assert prev["grading_pending"]["graded"] == 3
    assert prev["grading_pending"]["total"] == len(items)

    heal()
    events = _events(client, auth, sid)
    # 进度接着数，不从 0 重来
    assert events[0]["done"] == 3 and events[0]["resumed"] is True
    graded_seqs = [e["seq"] for e in events if e["type"] == "item" and "seq" in e]
    assert len(graded_seqs) == len(items) - 3            # 只批没批过的
    assert len(set(graded_seqs)) == len(graded_seqs)

    report = next(e for e in events if e["type"] == "report")["report"]
    assert report["count"] == len(items)
    # 全卷答 B，得分只由对错决定，但至少不能是"断点之后全是 0"
    assert report["score"] == _submit_free_score(items)


def _submit_free_score(items: list[dict]) -> float:
    """全卷答 B 时应得的分：选择题答案就是 B，综合题（数值题）答 B 必错。"""
    return round(sum(it["score"] for it in items if it["section"] == "choice"), 1)


def test_resumed_grading_does_not_double_write_the_error_book(client, auth, monkeypatch, no_llm):
    """续阅卷不能把批过的题再判一遍 —— 错题本会被同一场模考灌两份。

    错题本是薄弱画像和复习计划的输入，重复记录会让"这个知识点错了两次"变成假信号。
    """
    _full_bank()
    s = client.post("/exam/start", headers=auth).json()
    sid, items = s["session_id"], s["items"]
    client.post("/exam/save", headers=auth, json={
        "session_id": sid, "answers": {str(it["seq"]): "A" for it in items}})   # 全错

    heal = _crash_after(monkeypatch, 2)
    with pytest.raises(RuntimeError):
        _events(client, auth, sid)
    assert len(db.list_error_records(USER)) == 2

    heal()
    _submit(client, auth, sid)
    records = db.list_error_records(USER)
    assert len(records) == len(items)                    # 一题一条，不是 2 + 全部
    assert len({r["problem_id"] for r in records}) == len(items)


def test_history_lists_past_exams_with_scores(client, auth):
    """考过哪几场、各多少分 —— 成绩单一直存着，缺的只是这个列表入口。"""
    _full_bank()
    assert client.get("/exam/history", headers=auth).json()["items"] == []

    s = client.post("/exam/start", headers=auth).json()
    sid = s["session_id"]
    # 还在考的那场不算历史
    assert client.get("/exam/history", headers=auth).json()["items"] == []

    client.post("/exam/save", headers=auth, json={
        "session_id": sid, "answers": {str(it["seq"]): "B" for it in s["items"]}})
    report = _submit(client, auth, sid)

    rows = client.get("/exam/history", headers=auth).json()["items"]
    assert [r["session_id"] for r in rows] == [sid]
    assert rows[0]["graded"] is True
    assert rows[0]["score"] == report["score"]
    assert rows[0]["full_score"] == report["full_score"]
    assert rows[0]["submitted_at"]

    intruder = {"Authorization": f"Bearer {issue_token(OTHER, 'student')}"}
    assert client.get("/exam/history", headers=intruder).json()["items"] == []


def test_abandoned_exam_is_history_but_never_pending_grading(client, auth):
    """放弃的场次不是"没阅完"，别在开局提示考生去续一场他自己扔掉的卷子。"""
    _full_bank()
    sid = client.post("/exam/start", headers=auth).json()["session_id"]
    client.post("/exam/abandon", json={"session_id": sid}, headers=auth)

    prev = client.get("/exam/preview", headers=auth).json()
    assert prev["pending"] is None and prev["grading_pending"] is None
    row = client.get("/exam/history", headers=auth).json()["items"][0]
    assert row["session_id"] == sid and row["state"] == "abandoned" and row["graded"] is False
