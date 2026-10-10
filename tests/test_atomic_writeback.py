"""真实SQLite并发回归：非进程内锁、同账号累积、失败回滚与连接复用。"""
import concurrent.futures
import threading
import time
import multiprocessing
import os
import sqlite3
import pytest
from fastapi import HTTPException
from backend.core import db
from backend.core.config import settings
from backend.api.core_routes import _writeback
from backend.learning.mastery import update_mastery

@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "atomic.db"))
    db.init_db()
    kid = db.upsert_knowledge_point("os", "内存", "页面置换")
    pid = db.insert_problem("os", "选择题", "哪项是LRU？", 2, "B", "numeric", knowledge="页面置换")
    db.link_problem_knowledge(pid, kid)
    db.upsert_mastery("atomic-student", kid, .2618, .1, "learning")
    return kid, pid, dict(course_id="os", knowledge="页面置换", stem="哪项是LRU？", difficulty=2)

def test_same_account_parallel_answers_preserve_every_mastery_increment(sample, monkeypatch):
    kid, pid, full = sample
    original = db.get_mastery
    def slowed_read(*args):
        row = original(*args)
        time.sleep(.01)  # 原实现这里没有事务，确保线程交错；事务实现仍须正确。
        return row
    monkeypatch.setattr(db, "get_mastery", slowed_read)
    barrier = threading.Barrier(10)
    def answer(_):
        barrier.wait(timeout=10)
        _writeback("atomic-student", full, {"passed":True}, "", "", problem_id=pid, answer_text="B")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(answer, range(10)))
    expected = .2618
    for _ in range(10):
        expected = update_mastery(expected, True, difficulty=2)
    assert db.count_attempts("atomic-student", kid) == 10
    assert original("atomic-student", kid)["mastery"] == expected

def test_failed_mastery_write_rolls_back_attempt_and_snapshot(sample, monkeypatch):
    kid, pid, full = sample
    def fail(*args, **kwargs):
        raise RuntimeError("deliberate rollback regression")
    monkeypatch.setattr(db, "upsert_mastery", fail)
    with pytest.raises(HTTPException) as failure:
        _writeback("atomic-student", full, {"passed":True}, "", "", problem_id=pid, answer_text="B")
    assert failure.value.status_code == 503
    assert db.count_attempts("atomic-student", kid) == 0
    assert db.get_mastery("atomic-student", kid)["mastery"] == .2618


def _process_answers(path, pid, full, barrier):
    settings.db_path = path
    barrier.wait(timeout=20)
    for _ in range(5):
        _writeback("atomic-student", full, {"passed":True}, "", "", problem_id=pid, answer_text="B")


def test_atomic_mastery_across_separate_processes(sample):
    kid, pid, full = sample
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    workers = [context.Process(target=_process_answers, args=(settings.db_path,pid,full,barrier)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        if worker.is_alive():
            worker.terminate()
            worker.join()
        assert worker.exitcode == 0
    expected = .2618
    for _ in range(20):
        expected = update_mastery(expected, True, difficulty=2)
    assert db.count_attempts("atomic-student", kid) == 20
    assert db.get_mastery("atomic-student", kid)["mastery"] == expected


def test_helpers_reuse_transaction_without_early_commit(sample):
    kid, _, _ = sample
    with db.transaction() as outer:
        with db.get_conn() as inner:
            assert outer is inner
        db.save_attempt("atomic-student", "B", 100, knowledge_id=kid)
        with sqlite3.connect(settings.db_path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM practice_attempts").fetchone()[0] == 0
    assert db.count_attempts("atomic-student", kid) == 1
    with db.get_conn() as connection:
        assert connection is not outer


def test_nested_transaction_savepoint_rollback(sample):
    kid, _, _ = sample
    with db.transaction():
        db.save_attempt("atomic-student", "outer", 100, knowledge_id=kid)
        with pytest.raises(ValueError):
            with db.transaction():
                db.save_attempt("atomic-student", "inner", 100, knowledge_id=kid)
                raise ValueError("rollback inner only")
        assert db.count_attempts("atomic-student", kid) == 1
    assert db.count_attempts("atomic-student", kid) == 1


def test_migration_is_idempotent_and_preserves_attempts(sample):
    kid, _, _ = sample
    db.save_attempt("atomic-student", "kept", 100, knowledge_id=kid)
    db.init_db()
    db.init_db()
    assert db.count_attempts("atomic-student", kid) == 1
    with db.get_conn() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM practice_attempts WHERE username=?", ("atomic-student",)).fetchall()
        assert any("USING INDEX idx_attempts_user_" in row["detail"] for row in plan)


def test_recent_duration_uses_partial_covering_index_and_preserves_median(sample):
    kid, _, _ = sample
    for duration in (12, 0, 24, 36, 0):
        db.save_attempt("atomic-student", "B", 100, knowledge_id=kid, duration=duration)
    db.save_attempt("other-student", "B", 100, knowledge_id=kid, duration=999)
    assert db.attempt_duration_median("atomic-student") == 24
    assert db.attempt_duration_median("atomic-student", limit=2) == 30
    assert db.attempt_duration_median("missing-student") == 0
    with db.get_conn() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT duration FROM practice_attempts "
                            "WHERE username=? AND duration>0 ORDER BY id DESC LIMIT ?",
                            ("atomic-student",60)).fetchall()
        assert any("idx_attempts_user_duration" in row["detail"] for row in plan)
        assert not any("TEMP B-TREE" in row["detail"] for row in plan)


def test_materialized_knowledge_scope_preserves_aggregate_result(sample):
    kid, pid, _ = sample
    db.save_attempt("atomic-student", "B", 100, knowledge_id=kid, problem_id=pid)
    db.save_attempt("atomic-student", "free", 0, knowledge_id=kid)
    with db.get_conn() as conn:
        params = {"u":"atomic-student", "course":"os"}
        prior_sql = db._KNOWLEDGE_STATS_SQL.replace("WITH kp AS MATERIALIZED (", "WITH kp AS (")
        before = [dict(row) for row in conn.execute(prior_sql,params)]
        after = [dict(row) for row in conn.execute(db._KNOWLEDGE_STATS_SQL,params)]
    assert before == after
    point = next(row for row in after if row["knowledge_id"] == kid)
    assert point["attempts"] == 2
    assert point["correct_n"] == 1


def test_connection_gate_is_bounded_and_releases_after_connect_failure(sample, monkeypatch):
    key = (os.getpid(), os.path.realpath(settings.db_path))
    slots = db._CONNECTION_LIMITS[key]
    original = db._connect
    def fail_connect():
        raise sqlite3.OperationalError("injected connection failure")
    monkeypatch.setattr(db,"_connect",fail_connect)
    with pytest.raises(sqlite3.OperationalError):
        with db.get_conn():
            pass
    monkeypatch.setattr(db,"_connect",original)
    with db.get_conn() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    held = 0
    for _ in range(8):
        assert slots.acquire(blocking=False)
        held += 1
    try:
        assert not slots.acquire(blocking=False)
    finally:
        for _ in range(held):
            slots.release()


def test_short_queries_never_exceed_connection_gate(sample):
    guard = threading.Lock()
    counts = {"current":0,"maximum":0}
    def read(_):
        with db.get_conn() as conn:
            with guard:
                counts["current"] += 1
                counts["maximum"] = max(counts["maximum"],counts["current"])
            try:
                time.sleep(.02)
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            finally:
                with guard:
                    counts["current"] -= 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(read,range(20)))
    assert 1 <= counts["maximum"] <= 8
    assert counts["current"] == 0


def test_atomic_writer_has_separate_lane_when_read_slots_are_full(sample):
    kid, _, _ = sample
    key = (os.getpid(), os.path.realpath(settings.db_path))
    slots = db._CONNECTION_LIMITS[key]
    for _ in range(8):
        assert slots.acquire(blocking=False)
    try:
        with db.transaction():
            db.save_attempt("atomic-student", "writer", 100, knowledge_id=kid)
            assert db.count_attempts("atomic-student",kid) == 1
    finally:
        for _ in range(8):
            slots.release()
    assert db.count_attempts("atomic-student",kid) == 1


def test_request_scope_reuses_connection_but_preserves_helper_commit(sample):
    kid, _, _ = sample
    with db.connection_scope() as outer:
        with db.connection_scope() as nested:
            assert nested is outer
        with db.get_conn() as helper:
            assert helper is outer
        db.save_attempt("atomic-student", "scoped", 100, knowledge_id=kid)
        with sqlite3.connect(settings.db_path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM practice_attempts").fetchone()[0] == 1
        with db.transaction() as writer:
            assert writer is not outer
            db.save_attempt("atomic-student", "atomic", 100, knowledge_id=kid)
            with db.get_conn() as helper:
                assert helper is writer
            with sqlite3.connect(settings.db_path) as observer:
                assert observer.execute("SELECT COUNT(*) FROM practice_attempts").fetchone()[0] == 1
        assert db.count_attempts("atomic-student",kid) == 2
    assert db._CONNECTION_SCOPE.get() is None


def test_request_scope_is_thread_local_and_restores_after_failure(sample):
    barrier = threading.Barrier(2)
    def read(_):
        with db.connection_scope() as connection:
            barrier.wait(timeout=5)
            with db.get_conn() as helper:
                assert helper is connection
            return connection
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        connections = list(pool.map(read,range(2)))
    assert connections[0] is not connections[1]
    with pytest.raises(ValueError):
        with db.connection_scope():
            raise ValueError("scoped request failure")
    assert db._CONNECTION_SCOPE.get() is None
    with db.get_conn() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_stale_tutor_sessions_and_question_bank_share_atomic_mastery(sample):
    from backend.agents import tutor_graph as tutor
    kid, pid, full = sample
    barrier = threading.Barrier(10)
    def answer(index):
        barrier.wait(timeout=10)
        if index % 2:
            sess = tutor.Session(id=f"stale-{index}", username="atomic-student", problem=full["stem"],
                                 course="os", knowledge="页面置换", mastery=0.0, attempts=1)
            tutor.apply_diagnosis(sess, "B", {"correct":True,"score":100})
        else:
            _writeback("atomic-student", full, {"passed":True}, "", "", problem_id=pid, answer_text="B")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(answer,range(10)))
    expected = .2618
    for _ in range(10):
        expected = update_mastery(expected,True,difficulty=2)
    assert db.count_attempts("atomic-student",kid) == 10
    assert db.get_mastery("atomic-student",kid)["mastery"] == expected
