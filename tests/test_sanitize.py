"""模型输出清洗（测试报告 I-02：学习大厅历史回答顶部出现「---- 回答完毕 ----」）。

两部分：
1. 规则本身 —— 报告里的原样例要删干净；题库里真实存在的正常句子、表格、代码块不能误伤；
2. 大厅链路 —— 模型吐出带标记的内容时，入库的、发回页面的（final 事件）、
   以及下一轮回灌给模型的历史，三处都必须是干净的。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import backend.api.main as main_mod
from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.sanitize import clean_answer, clean_reasoning, strip_markers
from backend.core.security import issue_token
from backend.rag.retriever import Hit


# ---------- 1. 规则 ----------

@pytest.mark.parametrize("raw, expected", [
    # 报告里的两个原样例
    ("---- 回答完毕 ----\n栈是后进先出结构[1]。", "栈是后进先出结构[1]。"),
    ("证据引用处理完毕\n\n栈是 LIFO。\n\n---- 回答完毕 ----", "栈是 LIFO。"),
    # 思考块、孤立的结束标签、特殊 token、流结束符
    ("<think>先回忆定义</think>答案是 3。<|im_end|>", "答案是 3。"),
    ("我先想想\n</think>\n答案是 3。", "答案是 3。"),
    ("正文\n\n[DONE]", "正文"),
    ("<answer>队列先进先出</answer>", "队列先进先出"),
    ("【输出结束】\n队列先进先出", "队列先进先出"),
    # 模型按 grounded-QA 契约吐了整段 JSON：只取 answer
    ('{"answer": "队列先进先出[1]", "evidence": ["先进先出"]}', "队列先进先出[1]"),
])
def test_markers_are_removed(raw, expected):
    assert clean_answer(raw) == expected


@pytest.mark.parametrize("text", [
    # 题库里真实存在的正常句子（problems.answer #6042 一类），只是碰巧含"处理完毕"
    "如果所有边处理完毕均未发现环，则图中无环。",
    # Markdown 表格的分隔行
    "| 算法 | 复杂度 |\n|----|----|\n| 快排 | O(n log n) |",
    # 代码块里的 --- 和"回答完毕"是内容
    "```\n---\n回答完毕\n```",
    # 正文中间的分割线保留
    "第一部分\n\n---\n\n第二部分",
    "遍历完毕后输出结果。",
])
def test_normal_content_is_untouched(text):
    assert clean_answer(text) == text


def test_strip_markers_keeps_json_intact():
    """llm.chat() 的所有返回都过 strip_markers，诊断 / 出题的 JSON 不能被改坏。"""
    raw = '{"diagnosis": "所有边处理完毕后才判环", "steps": ["---"]}'
    assert json.loads(strip_markers(raw)) == json.loads(raw)
    assert strip_markers("<think>x</think>\n" + raw) == raw


def test_reasoning_drops_prompt_echo():
    raw = "资料[1]讲了栈。\n要求 evidence 必须是原文子串，只输出 JSON 对象。\n所以答案是 LIFO。"
    assert clean_reasoning(raw) == "资料[1]讲了栈。\n所以答案是 LIFO。"


# ---------- 2. 大厅链路 ----------

COURSE = "ds-algo"
USER = "sanitize_tester"
DIRTY = ["---- 回答完毕 ----\n", "证据引用处理完毕\n", "栈是后进先出结构[1]。", "\n<|im_end|>"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "llm_base_url", "http://x")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_workhorse_model", "m")
    db.init_db()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER, 'student')}"}


class _Engine:
    def build_context(self, hits):
        return "\n\n".join(f"[{i}] 来源：{h.citation()}\n{h.text}" for i, h in enumerate(hits, 1))


def _stub_llm(monkeypatch, seen: list):
    def fake(msgs):
        seen.append(msgs)
        return iter([("content", d) for d in DIRTY])
    monkeypatch.setattr(main_mod.llm_mod, "ask_reasoner_stream", fake)


def _events(client, auth, body) -> list[dict]:
    with client.stream("POST", "/chat/stream", json=body, headers=auth) as r:
        text = "".join(r.iter_text())
    return [json.loads(p) for p in text.split("data: ")[1:]]


@pytest.mark.parametrize("with_evidence", [True, False])
def test_hall_answer_is_clean_in_db_and_final_event(client, auth, monkeypatch, with_evidence):
    hit = Hit(text="栈是一种后进先出的线性表。", score=0.9, source="数据结构教材",
              page_start=1, page_end=1, title_path="第三章", doc_type="textbook")
    monkeypatch.setattr(main_mod, "_rag_hits",
                        lambda *a, **k: ([hit], _Engine()) if with_evidence else (None, None))
    seen: list = []
    _stub_llm(monkeypatch, seen)

    events = _events(client, auth, {"message": "栈是什么", "course": COURSE})
    final = next(e for e in events if e["type"] == "final")
    assert final["text"] == "栈是后进先出结构[1]。"

    sid = next(e for e in events if e["type"] == "session")["id"]
    turns = client.get(f"/sessions/{sid}", headers=auth).json()["turns"]
    assert turns[-1]["content"] == "栈是后进先出结构[1]。"

    if with_evidence:
        system = seen[0][0]["content"]
        # 根因：大厅不再把 grounded-QA 的 JSON 契约当系统提示
        assert "JSON 对象" not in system and "evidence" not in system
        cite = next(e for e in events if e["type"] == "citation")
        assert "score" not in cite


def test_dirty_history_is_cleaned_before_replay(client, auth, monkeypatch):
    """老记录里存着标记，下一轮原样回灌的话模型会照着学。"""
    monkeypatch.setattr(main_mod, "_rag_hits", lambda *a, **k: (None, None))
    seen: list = []
    _stub_llm(monkeypatch, seen)
    history = [{"role": "user", "content": "栈是什么"},
               {"role": "assistant", "content": "---- 回答完毕 ----\n栈是 LIFO。"},
               {"role": "assistant", "content": "证据引用处理完毕"}]
    _events(client, auth, {"message": "那队列呢", "course": COURSE, "history": history})
    replay = [m["content"] for m in seen[0] if m["role"] == "assistant"]
    assert replay == ["栈是 LIFO。"]   # 只剩标记的那条整条丢掉
