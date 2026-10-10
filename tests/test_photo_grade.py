"""拍照批改端点测试：转写 → 诊断 → 确认入库。

视觉/推理两个 LLM 调用全部 monkeypatch 掉，所以不需要 .env 里的 API key 也能跑。
最要紧的是最后那条 `test_collect_record_is_visible_in_error_book` ——
它守的是 knowledge_id 留 0 会被 JOIN 静默滤掉的坑，那类错误不会抛异常，
只能靠断言抓（见 docs/18 §3.2）。
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token

USER = "photo_tester"
COURSE = "co"
KNOWLEDGE = "浮点数表示"

_FAKE_TRANSCRIPT = "第一步：设阶码为 $E$，尾数为 $M$。\n第二步：$E = 10000010$，故实际指数为 3。"
_FAKE_DIAGNOSIS = {
    "correct": False,
    "error_tags": ["concept_confuse"],
    "diagnosis": "移码偏置常数用错了，127 写成了 128。",
    "remediation": "IEEE 754 单精度阶码偏置是 127。",
}


def _png_data_url(payload: bytes = b"fake-png-bytes") -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例一个全新的临时库。db.get_conn 每次都重读 settings.db_path，所以改它就够了。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


@pytest.fixture()
def auth() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER, 'student')}"}


@pytest.fixture()
def vision_on(monkeypatch):
    """假装配好了视觉模型，并把两个 LLM 调用换成固定桩。"""
    monkeypatch.setattr(settings, "llm_base_url", "http://stub/v1")
    monkeypatch.setattr(settings, "llm_api_key", "stub-key")
    monkeypatch.setattr(settings, "llm_vision_model", "stub-vision")

    from backend.agents import photo_grader
    from backend.core import llm as llm_mod

    monkeypatch.setattr(photo_grader.llm_mod, "ask_vision",
                        lambda prompt, image, max_tokens=2048: _FAKE_TRANSCRIPT)

    def _fake_stream(messages):
        yield ("reasoning", "先看阶码……")
        yield ("content", json.dumps(_FAKE_DIAGNOSIS, ensure_ascii=False))

    monkeypatch.setattr(llm_mod, "ask_reasoner_stream", _fake_stream)


def _events(resp) -> list[dict]:
    out = []
    for part in resp.text.split("\n\n"):
        line = part.strip()
        if line.startswith("data:"):
            out.append(json.loads(line[len("data:"):].strip()))
    return out


# ---------- 降级与入参校验 ----------

def test_requires_login(client):
    r = client.post("/grade/photo/stream", json={"image": _png_data_url()})
    assert r.status_code == 401


def test_503_when_vision_not_configured(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_model", "")
    r = client.post("/grade/photo/stream", json={"image": _png_data_url()}, headers=auth)
    assert r.status_code == 503
    # 话术写给学生：说清"暂未开通"和替代路径，配置名只留给 /health 与 preflight（docs/34 §2.2）
    detail = r.json()["detail"]
    assert "暂未开通" in detail and "CL_" not in detail


def test_rejects_non_image_data_url(client, auth, vision_on):
    r = client.post("/grade/photo/stream",
                    json={"image": "data:application/pdf;base64,AAAA"}, headers=auth)
    assert r.status_code == 400


def test_rejects_oversized_image(client, auth, vision_on):
    # 超过 _IMG_MAX_CHARS 的 base64 文本，在解码之前就该被挡下来
    huge = "data:image/png;base64," + "A" * 3_000_001
    r = client.post("/grade/photo/stream", json={"image": huge}, headers=auth)
    assert r.status_code == 400


def test_rejects_corrupt_base64(client, auth, vision_on):
    r = client.post("/grade/photo/stream",
                    json={"image": "data:image/png;base64,!!!not-base64!!!"}, headers=auth)
    assert r.status_code == 400


# ---------- 正常流程 ----------

def test_stream_stages_and_result(client, auth, vision_on):
    r = client.post("/grade/photo/stream",
                    json={"image": _png_data_url(), "course": COURSE,
                          "problem": "求该浮点数的真值"},
                    headers=auth)
    assert r.status_code == 200
    evs = _events(r)
    kinds = [e["type"] for e in evs]

    assert kinds[0] == "transcribing"
    assert kinds[-1] == "done"
    assert "transcript" in kinds and "diagnosing" in kinds and "result" in kinds
    # 转写必须排在诊断前面 —— 这个顺序就是这个端点做成流式的全部理由
    assert kinds.index("transcript") < kinds.index("diagnosing")

    transcript_ev = next(e for e in evs if e["type"] == "transcript")
    assert transcript_ev["text"] == _FAKE_TRANSCRIPT

    result = next(e for e in evs if e["type"] == "result")
    assert result["correct"] is False
    assert result["error_tags"] == ["concept_confuse"]
    assert result["transcript"] == _FAKE_TRANSCRIPT
    # finalize 会把标签翻成中文名，前端直接显示这个
    assert result["error_tag_names"]


def test_stream_does_not_write_error_record(client, auth, vision_on):
    """批改本身不落库——入库必须过学生确认那一手。"""
    client.post("/grade/photo/stream",
                json={"image": _png_data_url(), "course": COURSE}, headers=auth)
    assert db.list_error_records(USER, course=COURSE) == []


# ---------- 确认入库 ----------

def test_collect_rejects_empty_knowledge_point(client, auth):
    r = client.post("/grade/photo/collect",
                    json={"course": COURSE, "error_type": "concept_confuse",
                          "diagnosis": "x", "knowledge_point": "  "}, headers=auth)
    assert r.status_code == 400


def test_collect_rejects_unknown_course(client, auth):
    r = client.post("/grade/photo/collect",
                    json={"course": "no-such-course", "knowledge_point": KNOWLEDGE,
                          "error_type": "concept_confuse", "diagnosis": "x"}, headers=auth)
    assert r.status_code == 400


def test_collect_record_is_visible_in_error_book(client, auth):
    """守 knowledge_id 那个坑：list_error_records 是 JOIN knowledge_points 的内连接，
    knowledge_id=0 会被静默滤掉——存了等于没存，且不报任何错。"""
    r = client.post("/grade/photo/collect",
                    json={"course": COURSE, "knowledge_point": KNOWLEDGE,
                          "error_type": "concept_confuse",
                          "diagnosis": _FAKE_DIAGNOSIS["diagnosis"],
                          "problem_stem": _FAKE_TRANSCRIPT},
                    headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["knowledge_id"] > 0

    records = db.list_error_records(USER, course=COURSE)
    assert len(records) == 1
    assert records[0]["error_type"] == "concept_confuse"
    assert records[0]["knowledge_id"] == body["knowledge_id"]
    # problem_id=0 是有意的（不是题库题），不该影响可见性
    assert records[0]["problem_id"] == 0


def test_collect_is_scoped_to_the_caller(client, auth, vision_on):
    """两个账号同时演示时不能串号。"""
    client.post("/grade/photo/collect",
                json={"course": COURSE, "knowledge_point": KNOWLEDGE,
                      "error_type": "concept_confuse", "diagnosis": "x"}, headers=auth)
    other = {"Authorization": f"Bearer {issue_token('someone_else', 'student')}"}
    assert db.list_error_records("someone_else", course=COURSE) == []
    assert len(db.list_error_records(USER, course=COURSE)) == 1
    assert other  # 令牌确实是另一个人的
