"""数据体检的两条错位不变量。

`clear_problem_bank` 删该门全部题目并重置 `sqlite_sequence`，新题 id 从 1 重新开始；
而 practice_attempts / error_records 的 problem_id 没有外键，清库不会连带删作答。
"先有作答、后重建题库"于是让旧作答**改指向另一道题**——页面照常渲染，
原有四条不变量全部通过。这两条就是为了让体检在那种时候真的响。

用例必须造出错位再断言体检失败：一条恒为 0 的不变量和没有是一样的。
"""
from __future__ import annotations

import pytest

from backend.core import db
from backend.core.config import settings

from scripts import check_data

USER = "checkdata_tester"


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """一道 ds-algo 题 + 一条指向它的作答与错题，本身是干净的。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()

    kid = db.upsert_knowledge_point("ds-algo", "第一章", "快速排序")
    pid = db.insert_problem("ds-algo", "选择题", "快排的平均复杂度", 2, "答案", "rule",
                            knowledge="快速排序")
    db.link_problem_knowledge(pid, kid)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO practice_attempts(username,problem_id,answer,score,"
                     "hints_used,duration,created_at,knowledge_id,session_id) "
                     "VALUES(?,?,'答',100,0,10,'2026-08-01 10:00:00',?,0)", (USER, pid, kid))
        conn.execute("INSERT INTO error_records(username,problem_id,knowledge_id,error_type,"
                     "diagnosis,resolved,created_at) "
                     "VALUES(?,?,?,'boundary','边界写错',0,'2026-08-01 10:00:00')",
                     (USER, pid, kid))
    return {"kid": kid, "pid": pid}


def _run(monkeypatch) -> int:
    monkeypatch.setattr("sys.argv", ["check_data", "--quiet"])
    return check_data.main()


def test_clean_database_passes(seeded, monkeypatch):
    assert _run(monkeypatch) == 0


def test_attempt_pointing_at_a_deleted_problem_is_caught(seeded, monkeypatch, capsys):
    """题库被重建、作答没跟着清 —— 指向的题已经不存在了。"""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM problems WHERE id=?", (seeded["pid"],))

    assert _run(monkeypatch) != 0
    out = capsys.readouterr().out
    assert "指向已删除题目的作答" in out
    assert "指向已删除题目的错题" in out


def test_knowledge_of_another_course_is_caught(seeded, monkeypatch, capsys):
    """整库重建后 id 跨四门重排的典型症状：题还在，但已经是别门的题了。"""
    co_kid = db.upsert_knowledge_point("co", "指令系统", "寻址方式")
    with db.get_conn() as conn:
        # 作答仍指向那道 ds-algo 题，知识点却成了计组的
        conn.execute("UPDATE practice_attempts SET knowledge_id=? WHERE username=?",
                     (co_kid, USER))
        conn.execute("UPDATE error_records SET knowledge_id=? WHERE username=?",
                     (co_kid, USER))

    assert _run(monkeypatch) != 0
    out = capsys.readouterr().out
    assert "作答的知识点与题目课程错配" in out
    assert "错题的知识点与题目课程错配" in out


def test_free_qa_records_without_problem_id_are_not_flagged(seeded, monkeypatch):
    """problem_id=0 的自由问答错题本来就没有题号，不该被当成错位。"""
    with db.get_conn() as conn:
        conn.execute("INSERT INTO error_records(username,problem_id,knowledge_id,error_type,"
                     "diagnosis,resolved,created_at) "
                     "VALUES(?,0,?,'concept_confuse','概念混淆',0,'2026-08-02 10:00:00')",
                     (USER, seeded["kid"]))
    assert _run(monkeypatch) == 0
