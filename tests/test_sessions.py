"""会话读回：对话存了就得能拿回来。

大厅问答与解题会话从建表起就在往 learning_sessions / dialog_turns 里写，但一直
没有读回的入口 —— 前端只靠 keep-alive 撑着，刷新、重登、换设备一律清零，
而库里其实什么都在。这组用例守住补上的那条路：

1. 同一段对话落在**同一行会话**上（原先每发一条消息就新建一次会话）；
2. 列表 / 详情 / 删除都按 username 归属校验，别人的一律 404；
3. 解题会话的状态机能从快照重建 —— 后端重启（进程内字典清空）之后还能接着答。

全程不碰 LLM：大厅走未配置 LLM 的降级分支，解题直接操作 tutor_graph。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.agents import tutor_graph as tutor
from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token

USER = "sess_tester"
OTHER = "sess_intruder"
COURSE = "os"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    # 未配 LLM：/chat/stream 走兜底分支，不发任何网络请求
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_text_base_url", "")
    db.init_db()
    tutor._SESSIONS.clear()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER, 'student')}"}


@pytest.fixture()
def other_auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(OTHER, 'student')}"}


def _chat(client: TestClient, auth: dict, message: str, session_id: int = 0) -> int:
    """发一条大厅消息，返回后端给的会话 id。"""
    body = {"message": message, "course": COURSE, "session_id": session_id}
    with client.stream("POST", "/chat/stream", json=body, headers=auth) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:") and '"session"' in line:
                import json
                return int(json.loads(line[5:])["id"])
    return 0


# ---------- 大厅问答 ----------
def test_chat_turns_stay_in_one_session(client, auth):
    """一段对话落在同一行会话上，而不是每条消息新开一次。"""
    sid = _chat(client, auth, "栈和队列的区别？")
    assert sid > 0
    again = _chat(client, auth, "那什么时候该用栈？", session_id=sid)
    assert again == sid

    items = client.get("/sessions", params={"course": COURSE, "intent": "chat"},
                       headers=auth).json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == sid
    assert items[0]["turns"] == 4                 # 两问两答
    assert items[0]["title"].startswith("栈和队列")   # 摘要取第一句学生问句


def test_session_detail_replays_question_and_answer(client, auth):
    sid = _chat(client, auth, "进程和线程的区别？")
    detail = client.get(f"/sessions/{sid}", headers=auth).json()
    assert detail["course_id"] == COURSE and detail["intent"] == "chat"
    roles = [t["role"] for t in detail["turns"]]
    assert roles == ["student", "assistant"]
    assert detail["turns"][0]["content"] == "进程和线程的区别？"
    # 降级回复也要落库，否则读回的会话只有问句、没有答案
    assert detail["turns"][1]["content"]
    assert detail["turns"][1]["verified"] is False


def test_other_users_session_is_invisible(client, auth, other_auth):
    sid = _chat(client, auth, "只有我能看到这条")
    assert client.get(f"/sessions/{sid}", headers=other_auth).status_code == 404
    assert client.delete(f"/sessions/{sid}", headers=other_auth).status_code == 404
    assert client.get("/sessions", headers=other_auth).json()["items"] == []
    # 也不能把轮次追加进别人的会话：不归自己的 session_id 一律另开一次
    mine = _chat(client, other_auth, "我另开一次", session_id=sid)
    assert mine != sid


def test_anonymous_chat_is_not_persisted(client):
    """匿名试用仍然可用，但不留痕 —— 没有 username 就没有归属，也就无从读回。"""
    with client.stream("POST", "/chat/stream", json={"message": "试试", "course": COURSE}) as r:
        body = "".join(r.iter_text())
    assert '"session"' not in body
    assert client.get("/sessions").status_code == 401


def test_delete_session_removes_turns(client, auth):
    sid = _chat(client, auth, "删掉我")
    assert client.delete(f"/sessions/{sid}", headers=auth).json()["deleted"] is True
    assert client.get(f"/sessions/{sid}", headers=auth).status_code == 404
    assert db.list_turns(sid) == []


def test_list_filters_by_course(client, auth):
    sid = _chat(client, auth, "这条在操作系统")
    other_course = client.post("/chat/stream", json={"message": "这条在计组", "course": "co"},
                               headers=auth)
    assert other_course.status_code == 200
    items = client.get("/sessions", params={"course": COURSE}, headers=auth).json()["items"]
    assert [i["id"] for i in items] == [sid]


# ---------- 解题会话 ----------
FAKE_SOLUTION = {"knowledge_points": ["时间复杂度"], "strategy": "逐层累加",
                 "steps": [{"index": 1, "action": "写出递推式", "basis": "定义"}],
                 "final_answer": "O(n log n)"}


@pytest.fixture()
def no_llm_solver(monkeypatch):
    """分级提示要用结构化解析，这里直接给一份，避免用例依赖 LLM。"""
    monkeypatch.setattr(tutor.solver_mod, "solve", lambda *a, **k: dict(FAKE_SOLUTION))


def _start_tutor(course: str = COURSE) -> str:
    return tutor.start(USER, "求 f(n) 的时间复杂度", course=course)["session_id"]


def test_tutor_session_survives_restart(client, auth, no_llm_solver):
    """进程内字典清空（等同后端重启）之后，会话仍能按 id 续上。"""
    sid = _start_tutor()
    tutor.hint(sid, username=USER)                 # 提示到第 1 级
    tutor._SESSIONS.clear()

    sess = tutor.get_owned(sid, USER)
    assert sess is not None
    assert sess.problem == "求 f(n) 的时间复杂度"
    assert sess.hint_level == 1
    assert tutor.get_owned(sid, OTHER) is None     # 别人拿到 id 也续不了


def test_tutor_resume_returns_problem_and_turns(client, auth, no_llm_solver):
    sid = _start_tutor()
    tutor.hint(sid, username=USER)
    tutor._SESSIONS.clear()

    r = client.post("/tutor/resume", json={"course": COURSE}, headers=auth).json()
    assert r["session_id"] == sid
    assert r["problem"] == "求 f(n) 的时间复杂度"
    assert r["hint_level"] == 1
    kinds = [t.get("kind") for t in r["turns"]]
    assert kinds[0] == "ask"                       # 开场白与 start 时同一句
    assert "hint" in kinds                         # 分级提示也读得回来
    # 题目本身不重复出现在对话流里（它显示在「当前题目」那一栏）
    assert all(t["role"] != "student" for t in r["turns"])


def test_tutor_resume_is_empty_without_history(client, other_auth):
    assert client.post("/tutor/resume", json={"course": COURSE}, headers=other_auth).json() == {}
