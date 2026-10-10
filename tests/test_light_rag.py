from __future__ import annotations

import sqlite3

from backend.rag.retriever import DisabledRagEngine, LightRagEngine


def _index(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE chunks USING fts5("
        "text,course UNINDEXED,doc_type UNINDEXED,source UNINDEXED,title_path UNINDEXED,"
        "page_start UNINDEXED,page_end UNINDEXED,chunk_index UNINDEXED,tokenize='trigram')"
    )
    conn.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)", [
        ("二叉树中序遍历按照左根右的顺序访问。", "ds-algo", "textbook", "算法教材", "树 / 遍历", 12, 12, 1),
        ("操作系统使用页面置换算法管理虚拟内存。", "os", "courseware", "操作系统课件", "虚拟内存", 8, 9, 2),
    ])
    conn.commit()
    conn.close()


def test_light_rag_retrieves_chinese_and_preserves_citation(tmp_path):
    path = tmp_path / "rag.db"
    _index(path)
    hits = LightRagEngine(str(path)).retrieve("二叉树的中序遍历是什么", course="ds-algo", top_n=3)
    assert hits
    assert hits[0].source == "算法教材"
    assert hits[0].page_start == 12
    payload = hits[0].to_dict()
    assert payload["excerpt"] == "二叉树中序遍历按照左根右的顺序访问。"
    assert "score" not in payload   # 检索分是内部排序用的，不下发给页面


def test_light_rag_course_isolation(tmp_path):
    path = tmp_path / "rag.db"
    _index(path)
    assert LightRagEngine(str(path)).retrieve("页面置换算法", course="ds-algo") == []


def test_disabled_rag_never_opens_a_model_or_corpus():
    engine = DisabledRagEngine()
    assert engine.has_corpus() is False
    assert engine.retrieve("任何问题") == []
