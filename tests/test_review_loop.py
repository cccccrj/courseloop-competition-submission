"""docs/34 核验出的几处断点，修完之后用例守住。

1. 变式题复测：生成变式题之后，学生的作答要按**变式题**判分（此前仍按原题）；
2. 复核上下文：错题记录带上题干、学生作答、入队原因，复核队列原样给出；
3. 对外话术：流式与非流式接口都不外发环境变量名、`.env`、异常原文，失败带错误编号；
4. 版本戳：/admin/runtime（仅管理员）报告自己是哪个提交；公开的 /health 不暴露内部信息。

全程不碰真实 LLM：出题与诊断都换成桩。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.agents import tutor_graph as tutor
from backend.api.main import app
from backend.core import db, errors
from backend.core.config import settings
from backend.core.llm import LLMNotConfigured
from backend.core.security import issue_token

USER = "loop_stu"
COURSE = "os"

FAKE_SOLUTION = {"knowledge_points": ["页面置换"], "strategy": "按访问序列模拟",
                 "steps": [{"index": 1, "action": "列出访问序列", "basis": "定义"}],
                 "final_answer": "缺页 9 次"}
VARIANT = {"stem": "变式：FIFO 置换，3 个页框，访问序列 1 2 3 4 1 2 5，缺页几次？",
           "answer": "缺页 7 次", "hint": "逐个模拟页框", "verified": True}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_text_base_url", "")
    db.init_db()
    tutor._SESSIONS.clear()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER, 'student')}"}


@pytest.fixture()
def ta() -> dict:
    return {"Authorization": f"Bearer {issue_token('loop_ta', 'ta')}"}


@pytest.fixture()
def stubs(monkeypatch):
    """解析、出题、诊断都换成桩；诊断桩把"拿哪道题判的"记下来。"""
    seen: list[dict] = []
    monkeypatch.setattr(tutor.solver_mod, "solve", lambda *a, **k: dict(FAKE_SOLUTION))
    monkeypatch.setattr(tutor.pg_mod, "generate", lambda *a, **k: dict(VARIANT))

    def fake_diagnose(problem, answer, reference="", **kw):
        seen.append({"problem": problem, "answer": answer, "reference": reference})
        right = answer.strip() == reference.strip()
        return {"correct": right, "error_tags": [] if right else ["reasoning"],
                "error_tag_names": [] if right else ["推理错误"], "first_wrong_step": 0 if right else 2,
                "diagnosis": "" if right else "第 2 步**漏掉了**页框已满的情况", "score": 100 if right else 0}

    monkeypatch.setattr(tutor.diag_mod, "diagnose", fake_diagnose)
    return seen


# ---------- 1. 变式题复测 ----------

def test_variant_becomes_the_problem_that_gets_graded(client, stubs):
    sid = tutor.start(USER, "LRU 置换，缺页几次？", course=COURSE)["session_id"]
    tutor.submit(sid, "缺页 8 次", username=USER)              # 原题答错
    r = tutor.variant(sid, username=USER)
    assert r["current_problem"] == VARIANT["stem"]
    assert r["origin_problem"] == "LRU 置换，缺页几次？"

    out = tutor.submit(sid, "缺页 7 次", username=USER)        # 按变式题的答案作答
    assert stubs[-1]["problem"] == VARIANT["stem"]             # 判的是变式题
    assert stubs[-1]["reference"] == VARIANT["answer"]         # 参考答案用变式题自带的
    assert out["correct"] is True


def test_variant_survives_restart_and_resume(client, auth, stubs):
    sid = tutor.start(USER, "LRU 置换，缺页几次？", course=COURSE)["session_id"]
    tutor.submit(sid, "缺页 8 次", username=USER)
    tutor.variant(sid, username=USER)
    tutor._SESSIONS.clear()                                    # 等同后端重启

    r = client.post("/tutor/resume", json={"course": COURSE}, headers=auth).json()
    assert r["problem"] == VARIANT["stem"]
    assert r["origin_problem"] == "LRU 置换，缺页几次？"
    assert r["resolved"] is False                              # 变式题还没答，不算解决


def test_failed_variant_generation_does_not_switch_problem(client, stubs, monkeypatch):
    monkeypatch.setattr(tutor.pg_mod, "generate", lambda *a, **k: {"stem": "", "verified": False})
    sid = tutor.start(USER, "原题", course=COURSE)["session_id"]
    r = tutor.variant(sid, username=USER)
    assert r.get("error")
    assert tutor.get_owned(sid, USER).problem == "原题"


# ---------- 2. 复核上下文 ----------

def test_error_record_keeps_stem_answer_and_reason(client, ta, stubs):
    sid = tutor.start(USER, "LRU 置换，缺页几次？", course=COURSE)["session_id"]
    tutor.submit(sid, "我觉得缺页 8 次", username=USER)

    q = client.get("/teaching/errors?reviewed=no", headers=ta).json()
    row = q["items"][0]
    assert row["problem_stem"] == "LRU 置换，缺页几次？"
    assert row["student_answer"] == "我觉得缺页 8 次"
    assert row["diagnosis"].startswith("第 2 步")               # 完整诊断，不再切掉题干后截断
    assert row["review_reason"] == "manual" and row["review_reason_label"]
    assert row["follow_up"]                                     # 错因带处置建议


def test_old_rows_fall_back_to_stem_in_diagnosis(client, ta):
    """老格式 "题干 || 诊断" 的行没有 problem_stem 列值，从 diagnosis 切出来。"""
    kid = db.upsert_knowledge_point(COURSE, "内存管理", "页面置换")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO error_records(username,problem_id,knowledge_id,error_type,diagnosis,"
                     "resolved,created_at) VALUES(?,?,?,?,?,?,?)",
                     (USER, 0, kid, "boundary", "旧题干 || 旧诊断", 0, "2026-09-01 10:00:00"))
    row = client.get("/teaching/errors", headers=ta).json()["items"][0]
    assert row["problem_stem"] == "旧题干" and row["diagnosis"] == "旧诊断"
    assert row["knowledge"] == "页面置换"


def test_queue_orders_by_reason_and_filters(client, ta):
    kid = db.upsert_knowledge_point(COURSE, "内存管理", "页面置换")
    db.save_error_record(USER, "reasoning", "普通", problem_stem="A", knowledge_id=kid)
    db.save_error_record(USER, "careless", "粗心", problem_stem="B", knowledge_id=kid,
                         review_reason="careless")
    db.save_error_record(USER, "reasoning", "申请", problem_stem="C", knowledge_id=kid,
                         review_reason="student_request")
    items = client.get("/teaching/errors?reviewed=no", headers=ta).json()["items"]
    assert [i["problem_stem"] for i in items] == ["C", "B", "A"]
    only = client.get("/teaching/errors?reason=careless", headers=ta).json()["items"]
    assert [i["problem_stem"] for i in only] == ["B"]
    assert client.get("/teaching/errors?reason=bogus", headers=ta).status_code == 400


def test_demo_placeholder_answer_is_not_shown_as_student_work(client, ta):
    kid = db.upsert_knowledge_point(COURSE, "内存管理", "页面置换")
    db.save_error_record(USER, "reasoning", "诊断", problem_stem="题", knowledge_id=kid,
                         student_answer="演示作答")
    assert client.get("/teaching/errors", headers=ta).json()["items"][0]["student_answer"] == ""


# ---------- 3. 对外话术 ----------

LEAKS = ("CL_", ".env", "Traceback", "LLMNotConfigured")


def _stream_text(client, path, body, headers) -> str:
    with client.stream("POST", path, json=body, headers=headers) as r:
        return "".join(r.iter_lines())


def test_chat_without_llm_does_not_teach_students_about_env(client, auth):
    text = _stream_text(client, "/chat/stream", {"message": "什么是页表", "course": COURSE}, auth)
    events = [json.loads(p) for p in text.split("data: ")[1:]]
    reply = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "AI 问答暂未开通" in reply
    for bad in LEAKS:
        assert bad not in reply


def test_chat_llm_failure_is_translated_with_error_id(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "http://x")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_workhorse_model", "m")

    def boom(*a, **k):
        raise LLMNotConfigured("请在项目根 .env 填写 CL_LLM_BASE_URL")
        yield  # noqa: unreachable —— 让它成为生成器

    import backend.api.main as main_mod
    monkeypatch.setattr(main_mod.llm_mod, "ask_reasoner_stream", boom)
    monkeypatch.setattr(main_mod, "_rag_hits", lambda *a, **k: (None, None))
    text = _stream_text(client, "/chat/stream", {"message": "什么是页表", "course": COURSE}, auth)
    events = [json.loads(p) for p in text.split("data: ")[1:]]
    err = next(e for e in events if e["type"] == "error")
    assert err["message"] == errors.AI_UNAVAILABLE and len(err["error_id"]) == 8
    for bad in LEAKS:
        assert bad not in text


def test_photo_not_configured_detail_is_business_language(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_model", "")
    png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
           "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    r = client.post("/grade/photo/stream", json={"image": png, "course": COURSE}, headers=auth)
    assert r.status_code == 503
    for bad in LEAKS:
        assert bad not in r.text


def test_unhandled_exception_returns_error_id_not_traceback(client, auth, monkeypatch):
    import backend.api.core_routes as routes

    def boom(*a, **k):
        raise RuntimeError("sqlite3.OperationalError: no such table secret_table")

    monkeypatch.setattr(routes.tutor, "hint", boom)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/tutor/hint", json={"session_id": "x"}, headers=auth)
    assert r.status_code == 500
    body = r.json()
    assert "secret_table" not in r.text and len(body["error_id"]) == 8


def test_error_report_classifies():
    assert errors.report(LLMNotConfigured("x"), "t")["kind"] == "not_configured"
    assert errors.report(TimeoutError(), "t")["kind"] == "timeout"
    assert errors.report(ValueError("x"), "t")["message"] == errors.GENERIC


# ---------- 4. 版本戳 ----------

def test_runtime_reports_build_for_admin_only(client):
    admin = {"Authorization": f"Bearer {issue_token('loop_admin', 'admin')}"}
    b = client.get("/admin/runtime", headers=admin).json()["build"]
    assert b["commit"] and "dirty" in b and b["started_at"]
    for role in ("student", "ta"):
        r = client.get("/admin/runtime", headers={"Authorization": f"Bearer {issue_token('x', role)}"})
        assert r.status_code == 403


def test_public_health_exposes_no_internals(client):
    """/health 不登录就能读：模型名、检索库名、提交号都不该出现（交付产品不暴露内部信息）。"""
    body = client.get("/health").json()
    assert body["status"] == "ok" and "vision_ready" in body
    for key in ("workhorse_model", "chroma_collection", "rag_mode", "build", "service"):
        assert key not in body
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ---------- 5. 学生申请人工复核（docs/35 P1-2） ----------

def test_student_can_flag_own_diagnosis_and_it_jumps_the_queue(client, auth, ta, stubs):
    kid = db.upsert_knowledge_point(COURSE, "内存管理", "页面置换")
    db.save_error_record(USER, "reasoning", "别人的普通错题", problem_stem="A", knowledge_id=kid)
    sid = tutor.start(USER, "LRU 置换，缺页几次？", course=COURSE)["session_id"]
    out = tutor.submit(sid, "缺页 8 次", username=USER)
    rid = out["error_record_id"]
    assert rid

    r = client.post("/review-requests", json={"error_record_id": rid}, headers=auth)
    assert r.status_code == 200 and r.json()["requested_at"]
    top = client.get("/teaching/errors?reviewed=no", headers=ta).json()["items"][0]
    assert top["id"] == rid and top["review_reason"] == "student_request"

    mine = client.get(f"/records/errors?course={COURSE}", headers=auth).json()["items"]
    assert next(e for e in mine if e["id"] == rid)["review_requested"] is True


def test_cannot_flag_someone_elses_record(client, stubs):
    kid = db.upsert_knowledge_point(COURSE, "内存管理", "页面置换")
    rid = db.save_error_record("someone_else", "reasoning", "诊断", knowledge_id=kid)
    other = {"Authorization": f"Bearer {issue_token(USER, 'student')}"}
    assert client.post("/review-requests", json={"error_record_id": rid}, headers=other).status_code == 404


def test_hall_answer_request_round_trip(client, auth, ta):
    r = client.post("/review-requests", json={
        "course": COURSE, "source": "hall", "question": "页表放在哪里？",
        "ai_answer": "放在寄存器里", "no_evidence": True}, headers=auth)
    rid = r.json()["id"]

    open_items = client.get("/teaching/review-requests", headers=ta).json()["items"]
    assert open_items[0]["id"] == rid and open_items[0]["no_evidence"] is True
    assert client.get("/teaching/review-requests", headers=auth).status_code == 403

    # 需要更正却不写更正内容：挡住
    bad = client.post("/teaching/review-requests/reply",
                      json={"request_id": rid, "verdict": "corrected", "reply": ""}, headers=ta)
    assert bad.status_code == 400
    ok = client.post("/teaching/review-requests/reply",
                     json={"request_id": rid, "verdict": "corrected", "reply": "页表在内存里"}, headers=ta)
    assert ok.json()["status"] == "answered"

    mine = client.get(f"/review-requests?course={COURSE}", headers=auth).json()["items"]
    assert mine[0]["ta_reply"] == "页表在内存里" and "username" not in mine[0]
    # 不是错题：不进学生的错题本
    assert client.get(f"/records/errors?course={COURSE}", headers=auth).json()["items"] == []


def test_hall_no_evidence_answer_is_marked(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "http://x")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_workhorse_model", "m")
    import backend.api.main as main_mod
    monkeypatch.setattr(main_mod, "_rag_hits", lambda *a, **k: (None, None))
    monkeypatch.setattr(main_mod.llm_mod, "ask_reasoner_stream",
                        lambda *a, **k: iter([("content", "页表在内存里。")]))
    text = _stream_text(client, "/chat/stream", {"message": "页表在哪", "course": COURSE}, auth)
    events = [json.loads(p) for p in text.split("data: ")[1:]]
    ver = next(e for e in events if e["type"] == "verify")
    assert ver["passed"] is False and ver["reason"] == "no_evidence"


def test_photo_collect_can_request_review(client, auth, ta):
    r = client.post("/grade/photo/collect", json={
        "course": COURSE, "knowledge_point": "页面置换", "error_type": "calculation",
        "diagnosis": "第二步算错", "problem_stem": "我的草稿：缺页 8 次", "problem": "LRU 缺页几次",
        "request_review": True}, headers=auth).json()
    top = client.get("/teaching/errors?reviewed=no", headers=ta).json()["items"][0]
    assert top["id"] == r["id"] and top["review_reason"] == "student_request"
    assert top["problem_stem"] == "LRU 缺页几次" and top["student_answer"] == "我的草稿：缺页 8 次"


# ---------- 6. 改判回写下游（docs/35 P1-4） ----------

def test_override_out_of_misconception_releases_state(client, ta):
    kid = db.upsert_knowledge_point(COURSE, "内存管理", "页面置换")
    db.save_attempt(USER, "答", 0, knowledge_id=kid)
    db.upsert_mastery(USER, kid, 0.5, 0.3, "misconception")
    before = db.get_mastery(USER, kid)
    rid = db.save_error_record(USER, "concept_confuse", "混淆了 LRU 与 FIFO", knowledge_id=kid)

    r = client.post("/teaching/review-error", json={
        "record_id": rid, "verdict": "override", "error_type": "calculation"}, headers=ta).json()
    after = db.get_mastery(USER, kid)
    assert after["state"] != "misconception"
    assert after["mastery"] == before["mastery"]                 # 数值不动
    assert after["last_practiced_at"] == before["last_practiced_at"]  # 不算一次练习
    assert any("状态" in x for x in r["impact"])
    assert any("排期" in x for x in r["impact"])
    # 界面上"建议处置"要跟着新错因换，不能还挂着 AI 原判那条（UI 走查发现）
    assert r["follow_up"] and "对比" not in r["follow_up"]


def test_override_keeps_misconception_when_other_errors_support_it(client, ta):
    kid = db.upsert_knowledge_point(COURSE, "内存管理", "页面置换")
    db.save_attempt(USER, "答", 0, knowledge_id=kid)
    db.upsert_mastery(USER, kid, 0.5, 0.3, "misconception")
    db.save_error_record(USER, "knowledge_gap", "另一条", knowledge_id=kid)
    rid = db.save_error_record(USER, "concept_confuse", "这一条", knowledge_id=kid)
    client.post("/teaching/review-error", json={
        "record_id": rid, "verdict": "override", "error_type": "calculation"}, headers=ta)
    assert db.get_mastery(USER, kid)["state"] == "misconception"


# ---------- 7. careless 举证门槛（docs/35 P2） ----------

def test_careless_gate_unit():
    from backend.learning.error_taxonomy import enforce_careless_gate
    assert enforce_careless_gate(["careless", "boundary"], False) == ["boundary"]
    assert enforce_careless_gate(["careless"], False) == ["calculation"]
    assert enforce_careless_gate(["careless"], True) == ["careless"]
    assert enforce_careless_gate(["reasoning"], False) == ["reasoning"]


def test_careless_without_prior_success_is_replaced(client, stubs, monkeypatch):
    def careless_diag(problem, answer, reference="", **kw):
        return {"correct": False, "error_tags": ["careless", "boundary"],
                "error_tag_names": ["粗心失误", "边界遗漏"], "diagnosis": "抄错了", "score": 0}
    monkeypatch.setattr(tutor.diag_mod, "diagnose", careless_diag)
    sid = tutor.start(USER, "题", course=COURSE)["session_id"]
    out = tutor.submit(sid, "错答", username=USER)
    assert out["error_tags"][0] == "boundary"
    with db.get_conn() as conn:
        assert conn.execute("SELECT error_type FROM error_records").fetchone()[0] == "boundary"


# ---------- 8. 个性化练习：核对不了的题要重出，且措辞分清原因 ----------

def _fake_llm(outputs):
    it = iter(outputs)
    return lambda msgs, **kw: json.dumps(next(it), ensure_ascii=False)


OPEN_Q = {"stem": "请简述数据结构的基本概念。", "type": "概念判断", "answer": "数据结构是……",
          "code": "", "func_name": "", "test_cases": []}
CHOICE_Q = {"stem": "下列哪项是线性结构？\nA. 树\nB. 队列\nC. 图\nD. 堆", "type": "选择题", "answer": "B",
            "code": "", "func_name": "", "test_cases": [],
            "final_answer_canonical": {"kind": "choice", "value": "B"}}


def test_generator_retries_unverifiable_when_required(monkeypatch):
    from backend.agents import practice_generator as pg
    monkeypatch.setattr(pg.llm_mod, "ask_practice", _fake_llm([OPEN_Q, CHOICE_Q]))
    out = pg.generate("线性表", course="ds-algo", max_retries=2, require_verified=True)
    assert out["verified"] is True and out["stem"].startswith("下列哪项")


def test_generator_still_shows_unverifiable_by_default(monkeypatch):
    """解题页的变式题沿用原行为：核对不了也展示（标"待验证"）。"""
    from backend.agents import practice_generator as pg
    monkeypatch.setattr(pg.llm_mod, "ask_practice", _fake_llm([OPEN_Q]))
    assert pg.generate("线性表", course="ds-algo")["verified"] is None


def test_practice_generate_explains_unverifiable_not_as_wrong(client, auth, monkeypatch):
    from backend.agents import practice_generator as pg
    monkeypatch.setattr(pg.llm_mod, "ask_practice", _fake_llm([OPEN_Q] * 3))
    kid = db.upsert_knowledge_point("ds-algo", "绪论", "数据结构基本概念")
    r = client.post("/practice/generate", json={"knowledge_point": "数据结构基本概念",
                                                 "knowledge_id": kid, "course": "ds-algo"},
                    headers=auth).json()
    assert r["verified"] is False and r["problem_id"] == 0
    assert "没法自动核对" in r["error"] and "未通过答案验证" not in r["error"]


def test_practice_generate_passes_existing_stems_to_avoid(client, auth, monkeypatch):
    from backend.agents import practice_generator as pg
    seen = {}

    def fake(msgs, **kw):
        seen["user"] = msgs[-1]["content"]
        return json.dumps(CHOICE_Q, ensure_ascii=False)

    monkeypatch.setattr(pg.llm_mod, "ask_practice", fake)
    kid = db.upsert_knowledge_point("ds-algo", "绪论", "数据结构基本概念")
    db.insert_problem("ds-algo", "概念判断", "请简述数据结构的定义及其与算法的关系。", 2, "……", "rule",
                      knowledge="数据结构基本概念")
    client.post("/practice/generate", json={"knowledge_point": "数据结构基本概念",
                                             "knowledge_id": kid, "course": "ds-algo"}, headers=auth)
    assert "不得重复" in seen["user"] and "请简述数据结构的定义" in seen["user"]


def test_practice_generate_stream_reports_real_progress(client, auth, monkeypatch):
    """流式出题：页面看到的每一步都对应系统真实做的事（检索 / 出题 / 核对 / 重出 / 结果）。"""
    from backend.agents import practice_generator as pg
    outs = iter([OPEN_Q, CHOICE_Q])

    def fake_stream(task, msgs, **kw):
        text = json.dumps(next(outs), ensure_ascii=False)
        for i in range(0, len(text), 7):
            yield text[i:i + 7]

    monkeypatch.setattr(pg.llm_mod, "ask_task_stream", fake_stream)
    kid = db.upsert_knowledge_point("ds-algo", "线性表", "线性表")
    text = _stream_text(client, "/practice/generate/stream",
                        {"knowledge_point": "线性表", "knowledge_id": kid, "course": "ds-algo"}, auth)
    events = [json.loads(p) for p in text.split("data: ")[1:]]
    stages = [e.get("stage") for e in events if e["type"] == "stage"]
    assert stages == ["retrieve", "generate", "verify", "retry", "generate", "verify"]
    # 重出的原因码（unverifiable 等）只进日志，不下发：页面上只有一句中性的话
    retry = next(e for e in events if e.get("stage") == "retry")
    assert "reason" not in retry and retry["label"].endswith("换一道")
    assert sum(1 for e in events if e["type"] == "token") > 5
    assert not [e for e in events if e["type"] == "error"], [e for e in events if e["type"] == "error"]
    assert any(e["type"] == "result" for e in events), [e["type"] for e in events if e["type"] != "token"] + [text[-600:]]
    result = next(e for e in events if e["type"] == "result")["problem"]
    assert result["verified"] is True and result["problem_id"] > 0 and result["type"] == "选择题"
    assert events[-1]["type"] == "done"


# ---------- 9. 解析预热（第一次要提示不再等 20 多秒） ----------

def test_solution_is_prefetched_at_start_and_computed_once(client, monkeypatch):
    import threading
    calls = []
    gate = threading.Event()
    entered = threading.Event()

    def slow_solve(problem, **kw):
        calls.append(problem)
        entered.set()
        gate.wait(5)
        return dict(FAKE_SOLUTION)

    monkeypatch.setattr(tutor.solver_mod, "solve", slow_solve)
    monkeypatch.setattr(settings, "llm_base_url", "http://x")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    sid = tutor.start(USER, "预热的题", course=COURSE)["session_id"]
    assert entered.wait(5)
    assert calls == ["预热的题"]                 # 开始解题时就已经在后台算了
    gate.set()
    h = tutor.hint(sid, username=USER)          # 提示等后台那份，不再调一次
    assert h["hint"]["level"] == 1
    assert calls == ["预热的题"]


def test_prefetch_result_is_dropped_after_switching_to_variant(client, stubs, monkeypatch):
    import threading
    gate = threading.Event()
    entered = threading.Event()

    def slow_solve(problem, **kw):
        entered.set()
        gate.wait(5)
        return {**FAKE_SOLUTION, "final_answer": "原题的答案"}

    monkeypatch.setattr(tutor.solver_mod, "solve", slow_solve)
    monkeypatch.setattr(settings, "llm_base_url", "http://x")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    sid = tutor.start(USER, "原题", course=COURSE)["session_id"]
    assert entered.wait(5)
    future = tutor._PREFETCH[sid]
    tutor.variant(sid, username=USER)           # 预热还没算完就切到变式题
    gate.set()
    future.result(timeout=5)
    sess = tutor.get_owned(sid, USER)
    assert sess.problem == VARIANT["stem"]
    assert sess.solution["final_answer"] == VARIANT["answer"]   # 没被原题的解析覆盖


def test_queued_prefetch_keeps_schedule_time_problem(client, stubs, monkeypatch):
    """线程池繁忙时，原题任务直到切题后才启动，也不能覆盖变式答案。"""
    from concurrent.futures import Future
    queued = []

    class QueueOnlyExecutor:
        def submit(self, job):
            future = Future()
            queued.append((job, future))
            return future

    calls = []
    monkeypatch.setattr(tutor, "_PREFETCH_POOL", QueueOnlyExecutor())
    monkeypatch.setattr(tutor.solver_mod, "solve", lambda problem, **kw: calls.append(problem))
    monkeypatch.setattr(settings, "llm_base_url", "http://x")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    sid = tutor.start(USER, "尚未开始解析的原题", course=COURSE)["session_id"]
    tutor.variant(sid, username=USER)
    job, future = queued.pop()
    job()
    future.set_result(None)
    sess = tutor.get_owned(sid, USER)
    assert calls == []
    assert sess.problem == VARIANT["stem"]
    assert sess.solution["final_answer"] == VARIANT["answer"]


def test_build_commit_can_come_from_env_file_setting(monkeypatch):
    """代码包部署没有 .git：版本号写在 .env 的 CL_BUILD_COMMIT，由 settings 读到（docs/29 §3.2）。"""
    from backend.core import build_info
    monkeypatch.delenv("CL_BUILD_COMMIT", raising=False)
    monkeypatch.setattr(settings, "build_commit", "abc1234")
    build_info.build_info.cache_clear()
    try:
        info = build_info.build_info()
        assert info["commit"] == "abc1234" and info["source"] == "env" and info["dirty"] is False
    finally:
        build_info.build_info.cache_clear()
