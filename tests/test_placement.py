"""入学摸底 / 自适应分级测试。

这个功能的风险不在"能不能跑通"，在**它写进库的那几个数**：摸底给知识点写掌握度
初值，而掌握度是薄弱排序、错题销账、复习排期的共同输入，写错一处整条链路跟着歪。
所以用例主要压在四件事上：

1. 题池过滤 —— 不可确定性判定的题、没有知识点关联的题、串课的题都不能进摸底；
2. 定级口径 —— 档位与 profile 的难度表必须互为反函数，全错时不能一律判 0；
3. 落库边界 —— 只写测到的点、已有记录的点不覆盖、收尾只发生一次；
4. 会话 —— 状态在库里（刷新能续答），别人的会话答不了。

全程不碰 LLM：题池只收 verification_type='numeric'，判分走 check_numeric。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token
from backend.learning import placement
from backend.learning import profile as profile_mod

USER = "placement_tester"
OTHER = "placement_intruder"
COURSE = "co"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER, 'student')}"}


def _point(name: str, chapter: str, course: str = COURSE) -> int:
    return db.upsert_knowledge_point(course, chapter, name)


def _choice_problem(knowledge_id: int, knowledge: str, difficulty: int,
                    value: str = "B", course: str = COURSE, link: bool = True,
                    verification: str = "numeric", answer: str | None = None) -> int:
    """建一道可确定性判定的选择题并挂到知识点上。"""
    canonical = json.dumps({"kind": "choice", "value": value}, ensure_ascii=False)
    pid = db.insert_problem(course, "单项选择", f"[{knowledge}/{difficulty}] 选一个", difficulty,
                            answer if answer is not None else canonical, verification,
                            knowledge=knowledge)
    if link:
        db.link_problem_knowledge(pid, knowledge_id)
    return pid


def _bank(chapters: dict[str, list[str]], difficulties=(1, 2, 3, 4, 5)) -> dict[int, dict]:
    """按 {章: [知识点]} 建题库，每个知识点每档难度各一题。返回 problem_id -> 元信息。"""
    out: dict[int, dict] = {}
    for chapter, names in chapters.items():
        for name in names:
            kid = _point(name, chapter)
            for d in difficulties:
                pid = _choice_problem(kid, name, d)
                out[pid] = {"chapter": chapter, "knowledge": name, "knowledge_id": kid,
                            "difficulty": d}
    return out


def _start(client, auth, length=6, restart=False) -> dict:
    r = client.post("/placement/start",
                    json={"course": COURSE, "length": length, "restart": restart}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _answer(client, auth, session_id: int, text: str) -> dict:
    r = client.post("/placement/answer",
                    json={"session_id": session_id, "answer": text, "duration": 30}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 档位 ↔ 掌握度：与 profile 共用一把尺子 ----------

def test_mastery_for_level_is_inverse_of_target_difficulty():
    """摸底定的档位，必须正好是练习页拿这个初值算出来的目标难度。
    两处对不上，就会出现"摸底说你是进阶、练习页一直推最简单的题"。"""
    for level in (1, 2, 3, 4, 5):
        assert profile_mod._target_difficulty(placement.mastery_for_level(level)) == level


def test_level_zero_mastery_below_first_band():
    assert placement.mastery_for_level(0) < placement.mastery_for_level(1)
    assert profile_mod._target_difficulty(placement.mastery_for_level(0)) == 1


def _items(*pairs) -> list[dict]:
    return [{"difficulty": d, "correct": c} for d, c in pairs]


def test_level_of_all_correct_is_top_answered_band():
    assert placement.level_of(_items((1, True), (2, True), (3, True))) == 3
    assert placement.level_of(_items((5, True), (4, True))) == 5


def test_level_of_all_wrong_falls_back_to_one_below_easiest():
    """第 3 档做错只否定第 3 档。一律判 0 的话，绝大多数人摸完底都会被写成接近零。"""
    assert placement.level_of(_items((3, False), (4, False))) == 2
    assert placement.level_of(_items((1, False))) == 0


def test_level_of_skips_bands_answered_mostly_wrong():
    # 第 3 档两题全对、更高两档全错 —— 站得住第 3 档
    assert placement.level_of(_items((3, True), (3, True), (4, False), (5, False))) == 3
    # 高档全错、低档全对：以做对过的最高档为准
    assert placement.level_of(_items((1, True), (2, True), (4, False), (5, False))) == 2
    # 第 4 档三题只对一题，不算站住，退到做对过的下一档
    assert placement.level_of(
        _items((2, True), (4, True), (4, False), (4, False))) == 2


def test_level_of_empty_is_none_not_zero():
    """一道都没测到的知识点是"没数据"，不是"零分"。"""
    assert placement.level_of([]) is None


# ---------- 题池 ----------

def test_pool_excludes_unlinked_and_non_deterministic_problems(client):
    kid = _point("Cache映射与替换", "存储器层次结构")
    good = _choice_problem(kid, "Cache映射与替换", 3)
    _choice_problem(kid, "Cache映射与替换", 3, link=False)             # 没挂知识点
    _choice_problem(kid, "Cache映射与替换", 3, verification="rule")     # 判不了要回落 LLM
    _choice_problem(kid, "Cache映射与替换", 3, answer="")               # 没有 canonical
    pool = db.placement_pool(USER, COURSE)
    assert [p["problem_id"] for p in pool] == [good]


def test_pool_does_not_cross_courses(client):
    """同名知识点在两门课里各有一个 id，摸底不能把别门的题抽进来。"""
    kid_co = _point("性能指标", "计算机系统概述")
    kid_net = _point("性能指标", "计算机网络概述", course="net")
    mine = _choice_problem(kid_co, "性能指标", 2)
    _choice_problem(kid_net, "性能指标", 2, course="net")
    assert [p["problem_id"] for p in db.placement_pool(USER, COURSE)] == [mine]
    assert len(db.placement_pool(USER, "net")) == 1
    assert kid_co != kid_net


def test_pool_marks_already_attempted_problems(client):
    kid = _point("指令格式", "指令系统")
    seen = _choice_problem(kid, "指令格式", 2)
    fresh = _choice_problem(kid, "指令格式", 3)
    db.save_attempt(USER, "B", 100.0, problem_id=seen, knowledge_id=kid)
    flags = {p["problem_id"]: p["seen"] for p in db.placement_pool(USER, COURSE)}
    assert flags[seen] == 1 and flags[fresh] == 0


# ---------- 可用性 ----------

def test_status_reports_not_ready_when_bank_too_thin(client, auth):
    kid = _point("总线", "总线和输入输出系统")
    _choice_problem(kid, "总线", 2)
    r = client.get(f"/placement/status?course={COURSE}", headers=auth).json()
    assert r["ready"] is False and r["pool_size"] == 1
    assert r["min_items"] == placement.MIN_LENGTH
    # 不可用时开始摸底要给出可执行的说法，而不是 500
    bad = client.post("/placement/start", json={"course": COURSE}, headers=auth)
    assert bad.status_code == 503
    assert "客观题" in bad.json()["detail"]


def test_status_requires_login_and_known_course(client, auth):
    assert client.get("/placement/status").status_code == 401
    assert client.get("/placement/status?course=nope", headers=auth).status_code == 400


# ---------- 自适应选题 ----------

def test_difficulty_goes_up_on_correct_and_down_on_wrong(client, auth):
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    s = _start(client, auth, length=6)
    assert s["item"]["difficulty"] == placement.START_DIFFICULTY

    r = _answer(client, auth, s["session_id"], "B")        # 答对 → 目标 4
    assert r["correct"] is True and r["item"]["difficulty"] == 4
    r = _answer(client, auth, s["session_id"], "A")        # 答错 → 目标 3
    assert r["correct"] is False and r["item"]["difficulty"] == 3


def test_items_rotate_across_chapters(client, auth):
    _bank({"第一章": ["甲"], "第二章": ["乙"], "第三章": ["丙"]})
    s = _start(client, auth, length=6)
    seen = [s["item"]["chapter"]]
    sid = s["session_id"]
    for _ in range(2):
        seen.append(_answer(client, auth, sid, "B")["item"]["chapter"])
    assert set(seen) == {"第一章", "第二章", "第三章"}


def test_undetermined_answer_does_not_move_difficulty_or_count(client, auth):
    """作答解析不了时 check_numeric 返回 None。折成"答错"会既扣难度又进错题本。"""
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    s = _start(client, auth, length=6)
    r = _answer(client, auth, s["session_id"], "我不会")
    assert r["correct"] is None
    assert r["item"]["difficulty"] == placement.START_DIFFICULTY   # 难度没动
    assert db.count_attempts(USER, 0) == 0
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM practice_attempts WHERE username=?",
                            (USER,)).fetchone()[0] == 0


# ---------- 会话 ----------

def test_session_survives_reload_via_status(client, auth):
    """状态在库里而不是进程内存里：刷新页面要能接着答同一题。"""
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    s = _start(client, auth, length=6)
    nxt = _answer(client, auth, s["session_id"], "B")

    st = client.get(f"/placement/status?course={COURSE}", headers=auth).json()
    assert st["active"]["session_id"] == s["session_id"]
    assert st["active"]["answered"] == 1
    assert st["active"]["item"]["problem_id"] == nxt["item"]["problem_id"]
    # 再点一次「开始」是续答，不是重开
    again = _start(client, auth, length=6)
    assert again["resumed"] is True and again["session_id"] == s["session_id"]


def test_restart_abandons_previous_session(client, auth):
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    first = _start(client, auth, length=6)
    second = _start(client, auth, length=6, restart=True)
    assert second["session_id"] != first["session_id"]
    assert db.get_session(first["session_id"])["state"] == "abandoned"


def test_other_user_cannot_answer_my_session(client, auth):
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    s = _start(client, auth, length=6)
    intruder = {"Authorization": f"Bearer {issue_token(OTHER, 'student')}"}
    r = client.post("/placement/answer",
                    json={"session_id": s["session_id"], "answer": "B"}, headers=intruder)
    assert r.status_code == 404


# ---------- 收尾与落库 ----------

def _run_full(client, auth, length=6, answer_text="B") -> dict:
    s = _start(client, auth, length=length)
    sid = s["session_id"]
    last = None
    for _ in range(length):
        last = _answer(client, auth, sid, answer_text)
        if last.get("finished"):
            break
    assert last and last["finished"] is True
    return last["result"]


def test_finish_writes_initial_mastery_only_for_tested_points(client, auth):
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    _point("从未测到的点", "第三章")          # 建了知识点但没有题
    result = _run_full(client, auth, length=6)

    assert result["finished"] is True
    assert result["correct"] == 6 and result["level"] == 5
    written = {m["knowledge_id"] for m in db.list_mastery(USER, COURSE)}
    tested = {p["knowledge_id"] for p in result["points"]}
    assert written == tested
    assert result["coverage"]["points_tested"] == 2
    assert result["coverage"]["points_written"] == 2
    # 写进去的值就是档位反解出来的那个，不是另算的
    for row in db.list_mastery(USER, COURSE):
        assert row["mastery"] == placement.mastery_for_level(5)


def test_existing_mastery_is_not_overwritten_by_placement(client, auth):
    """摸底是冷启动工具。真实作答攒出来的估计比一道摸底题可靠，不能被冲掉。"""
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    kid = db.get_or_create_knowledge_id("甲", COURSE)
    db.upsert_mastery(USER, kid, 0.82, 0.6, "mastered")

    result = _run_full(client, auth, length=6, answer_text="A")   # 全错
    assert db.get_mastery(USER, kid)["mastery"] == 0.82
    row = next(p for p in result["points"] if p["knowledge_id"] == kid)
    assert row["skipped"] is True
    assert result["coverage"]["points_written"] < result["coverage"]["points_tested"]


def test_wrong_answers_land_in_error_book(client, auth):
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    _run_full(client, auth, length=6, answer_text="A")
    records = db.list_error_records(USER, course=COURSE)
    assert len(records) == 6
    # 选择题选错记"知识缺失"而不是"计算错误"——这个标签会被出题端当约束用
    assert {r["error_type"] for r in records} == {"knowledge_gap"}
    assert all(r["knowledge_id"] for r in records)


def test_finalize_is_idempotent(client, auth):
    """重复提交最后一题不能把掌握度和错题写两遍。"""
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    s = _start(client, auth, length=6)
    sid = s["session_id"]
    for _ in range(6):
        r = _answer(client, auth, sid, "A")
        if r.get("finished"):
            break
    before = len(db.list_error_records(USER, course=COURSE))

    again = client.post("/placement/answer", json={"session_id": sid, "answer": "A"}, headers=auth)
    assert again.status_code == 404          # 已结束的会话不再接受作答
    assert len(db.list_error_records(USER, course=COURSE)) == before
    # 报告是从轮次重算的，会话结束后照样拿得到
    assert placement.build_result(sid)["answered"] == 6


def test_cold_start_clears_after_placement(client, auth):
    """摸底存在的意义：做完之后，下游那一整条链路要立刻有东西可算。"""
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    before = client.get(f"/profile/weak?course={COURSE}", headers=auth).json()
    assert before["cold_start"] is True

    _run_full(client, auth, length=6)
    after = client.get(f"/profile/weak?course={COURSE}", headers=auth).json()
    assert after["cold_start"] is False
    rec = client.get(f"/practice/recommend?course={COURSE}", headers=auth).json()
    assert rec["cold_start"] is False


def test_result_reports_coverage_and_review(client, auth):
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    result = _run_full(client, auth, length=6)
    assert len(result["review"]) == 6
    assert all(r["answer"] == "B" for r in result["review"])      # canonical 转成可读写法
    assert result["coverage"]["points_total"] == len(
        __import__("backend.core.courses", fromlist=["get_course"]).get_course(COURSE).topics)
    assert result["level_name"] and result["level_hint"]
    assert result["next_difficulty"] == profile_mod._target_difficulty(result["level_mastery"])


def test_last_result_is_rebuilt_after_reload(client, auth):
    _bank({"第一章": ["甲"], "第二章": ["乙"]})
    result = _run_full(client, auth, length=6)
    st = client.get(f"/placement/status?course={COURSE}", headers=auth).json()
    assert st["active"] is None
    assert st["last"]["session_id"] == result["session_id"]
    assert st["last"]["level"] == result["level"]


def test_session_length_clamped_to_pool_size(client, auth):
    """题池比要求的题数还少时按题池来，不硬凑重复题。"""
    kid = _point("浮点数与IEEE754", "数据的表示和运算")
    for d in (1, 2, 3, 4, 5, 5, 5):
        _choice_problem(kid, "浮点数与IEEE754", d)
    s = _start(client, auth, length=20)
    assert s["total"] == 7
