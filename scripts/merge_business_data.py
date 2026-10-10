"""把一个 CourseLoop SQLite 的有用业务数据非破坏性合并到另一份库。

默认在事务末尾回滚，只输出合并预览；显式 ``--apply`` 才提交。题目与知识点
按业务键映射 ID，用户、作答、错题、掌握度、会话、复习计划和作业均保留。
目标库已有用户资料不被源库覆盖。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _insert(conn: sqlite3.Connection, table: str, row: sqlite3.Row | dict,
            *, omit: tuple[str, ...] = ("id",), overrides: dict | None = None) -> int:
    data = {k: row[k] for k in row.keys() if k not in omit}
    data.update(overrides or {})
    cols = list(data)
    sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})"
    return int(conn.execute(sql, [data[c] for c in cols]).lastrowid)


def _find(conn: sqlite3.Connection, table: str, fields: dict) -> sqlite3.Row | None:
    where = " AND ".join(f"{key} IS ?" for key in fields)
    return conn.execute(f"SELECT * FROM {table} WHERE {where} LIMIT 1", list(fields.values())).fetchone()


def _mapped(value: int | None, mapping: dict[int, int]) -> int:
    value = int(value or 0)
    return mapping.get(value, 0) if value else 0


def merge(source: Path, destination: Path, *, apply: bool = False) -> dict:
    src = _connect(source)
    dst = _connect(destination)
    stat: dict[str, int | bool | str] = {"applied": apply}
    try:
        dst.execute("BEGIN IMMEDIATE")

        # 用户：目标库是权威资料源，只补不存在的账号。
        added = 0
        for row in src.execute("SELECT * FROM users ORDER BY username"):
            if not dst.execute("SELECT 1 FROM users WHERE username=?", (row["username"],)).fetchone():
                _insert(dst, "users", row, omit=())
                added += 1
        stat["users_added"] = added

        # 知识点和题目先建立稳定映射，所有学习记录都通过映射写入。
        kp_map: dict[int, int] = {}
        added = 0
        for row in src.execute("SELECT * FROM knowledge_points ORDER BY id"):
            found = dst.execute(
                "SELECT id FROM knowledge_points WHERE course_id=? AND name=? LIMIT 1",
                (row["course_id"], row["name"]),
            ).fetchone()
            if found:
                kid = int(found["id"])
                dst.execute(
                    "UPDATE knowledge_points SET chapter=?,description=?,difficulty=? WHERE id=?",
                    (row["chapter"], row["description"], row["difficulty"], kid),
                )
            else:
                kid = _insert(dst, "knowledge_points", row)
                added += 1
            kp_map[int(row["id"])] = kid
        stat["knowledge_points_added"] = added

        problem_map: dict[int, int] = {}
        added = 0
        problem_fields = ("source", "type", "difficulty", "answer", "verification_type",
                          "func_name", "ref_code", "knowledge", "hint")
        for row in src.execute("SELECT * FROM problems ORDER BY id"):
            found = dst.execute(
                "SELECT id FROM problems WHERE course_id=? AND stem=? LIMIT 1",
                (row["course_id"], row["stem"]),
            ).fetchone()
            if found:
                pid = int(found["id"])
                dst.execute(
                    f"UPDATE problems SET {','.join(f'{x}=?' for x in problem_fields)} WHERE id=?",
                    [row[x] for x in problem_fields] + [pid],
                )
            else:
                pid = _insert(dst, "problems", row)
                added += 1
            problem_map[int(row["id"])] = pid
        stat["problems_added"] = added

        added = 0
        for row in src.execute("SELECT * FROM problem_knowledge"):
            fields = {"problem_id": _mapped(row["problem_id"], problem_map),
                      "knowledge_id": _mapped(row["knowledge_id"], kp_map)}
            if fields["problem_id"] and fields["knowledge_id"]:
                found = _find(dst, "problem_knowledge", fields)
                if found:
                    dst.execute("UPDATE problem_knowledge SET weight=? WHERE problem_id=? AND knowledge_id=?",
                                (row["weight"], fields["problem_id"], fields["knowledge_id"]))
                else:
                    _insert(dst, "problem_knowledge", row, omit=(), overrides=fields)
                    added += 1
        stat["problem_links_added"] = added

        for table, fingerprint in (
            ("test_cases", ("problem_id", "input", "expected_output", "hidden")),
            ("rubric_items", ("problem_id", "description", "score", "expected_evidence")),
        ):
            added = 0
            for row in src.execute(f"SELECT * FROM {table} ORDER BY id"):
                overrides = {"problem_id": _mapped(row["problem_id"], problem_map)}
                fields = {key: overrides.get(key, row[key]) for key in fingerprint}
                if fields["problem_id"] and not _find(dst, table, fields):
                    _insert(dst, table, row, overrides=overrides)
                    added += 1
            stat[f"{table}_added"] = added

        # 会话先于作答导入，使 session_id 也能正确映射。
        session_map: dict[int, int] = {}
        added = 0
        for row in src.execute("SELECT * FROM learning_sessions ORDER BY id"):
            fields = {"username": row["username"], "course_id": row["course_id"],
                      "problem_id": _mapped(row["problem_id"], problem_map),
                      "intent": row["intent"], "state": row["state"],
                      "started_at": row["started_at"], "completed_at": row["completed_at"]}
            found = _find(dst, "learning_sessions", fields)
            sid = int(found["id"]) if found else _insert(dst, "learning_sessions", row, overrides=fields)
            if not found:
                added += 1
            session_map[int(row["id"])] = sid
        stat["learning_sessions_added"] = added

        added = 0
        for row in src.execute("SELECT * FROM dialog_turns ORDER BY id"):
            fields = {"session_id": _mapped(row["session_id"], session_map), "role": row["role"],
                      "content": row["content"], "structured_data": row["structured_data"],
                      "created_at": row["created_at"]}
            if fields["session_id"] and not _find(dst, "dialog_turns", fields):
                _insert(dst, "dialog_turns", row, overrides=fields)
                added += 1
        stat["dialog_turns_added"] = added

        added = 0
        for row in src.execute("SELECT * FROM practice_attempts ORDER BY id"):
            fields = {"username": row["username"],
                      "problem_id": _mapped(row["problem_id"], problem_map),
                      "answer": row["answer"], "score": row["score"],
                      "hints_used": row["hints_used"], "duration": row["duration"],
                      "created_at": row["created_at"],
                      "knowledge_id": _mapped(row["knowledge_id"], kp_map),
                      "session_id": _mapped(row["session_id"], session_map)}
            if fields["problem_id"] and not _find(dst, "practice_attempts", fields):
                _insert(dst, "practice_attempts", row, overrides=fields)
                added += 1
        stat["practice_attempts_added"] = added

        added = 0
        error_fields = ("username", "problem_id", "knowledge_id", "error_type", "diagnosis",
                        "resolved", "created_at", "ta_reviewed", "ta_verdict", "ta_note",
                        "ta_username", "ta_reviewed_at", "ai_error_type", "resolved_at")
        for row in src.execute("SELECT * FROM error_records ORDER BY id"):
            overrides = {"problem_id": _mapped(row["problem_id"], problem_map),
                         "knowledge_id": _mapped(row["knowledge_id"], kp_map)}
            fields = {key: overrides.get(key, row[key]) for key in error_fields}
            if fields["problem_id"] and not _find(dst, "error_records", fields):
                _insert(dst, "error_records", row, overrides=overrides)
                added += 1
        stat["error_records_added"] = added

        added = updated = 0
        for row in src.execute("SELECT * FROM mastery_records"):
            fields = {"username": row["username"],
                      "knowledge_id": _mapped(row["knowledge_id"], kp_map)}
            found = _find(dst, "mastery_records", fields)
            if not found:
                _insert(dst, "mastery_records", row, omit=(), overrides=fields)
                added += 1
            elif (row["last_practiced_at"] or "") > (found["last_practiced_at"] or ""):
                cols = [k for k in row.keys() if k not in ("username", "knowledge_id")]
                dst.execute(f"UPDATE mastery_records SET {','.join(f'{x}=?' for x in cols)} "
                            "WHERE username=? AND knowledge_id=?",
                            [row[x] for x in cols] + [fields["username"], fields["knowledge_id"]])
                updated += 1
        stat["mastery_records_added"] = added
        stat["mastery_records_updated"] = updated

        added = 0
        for row in src.execute("SELECT * FROM mastery_snapshots ORDER BY id"):
            fields = {"username": row["username"],
                      "knowledge_id": _mapped(row["knowledge_id"], kp_map),
                      "mastery": row["mastery"], "state": row["state"],
                      "created_at": row["created_at"]}
            if fields["knowledge_id"] and not _find(dst, "mastery_snapshots", fields):
                _insert(dst, "mastery_snapshots", row, overrides=fields)
                added += 1
        stat["mastery_snapshots_added"] = added

        plan_map: dict[int, int] = {}
        added = 0
        for row in src.execute("SELECT * FROM review_plans ORDER BY id"):
            keys = ("username", "course_id", "goal", "start_date", "exam_date", "created_at")
            fields = {key: row[key] for key in keys}
            found = _find(dst, "review_plans", fields)
            plan_id = int(found["id"]) if found else _insert(dst, "review_plans", row)
            if not found:
                added += 1
            plan_map[int(row["id"])] = plan_id
        stat["review_plans_added"] = added

        task_map: dict[int, int] = {}
        added = 0
        for row in src.execute("SELECT * FROM review_tasks ORDER BY id"):
            fields = {"plan_id": _mapped(row["plan_id"], plan_map),
                      "knowledge_id": _mapped(row["knowledge_id"], kp_map),
                      "task_date": row["task_date"], "task_type": row["task_type"],
                      "day_index": row["day_index"], "review_round": row["review_round"],
                      "created_at": row["created_at"]}
            found = _find(dst, "review_tasks", fields)
            task_id = int(found["id"]) if found else _insert(dst, "review_tasks", row, overrides=fields)
            if not found:
                added += 1
            task_map[int(row["id"])] = task_id
        stat["review_tasks_added"] = added

        added = 0
        for row in src.execute("SELECT * FROM review_task_attempts"):
            fields = {"task_id": _mapped(row["task_id"], task_map),
                      "problem_id": _mapped(row["problem_id"], problem_map)}
            if all(fields.values()) and not _find(dst, "review_task_attempts", fields):
                _insert(dst, "review_task_attempts", row, omit=(), overrides=fields)
                added += 1
        stat["review_task_attempts_added"] = added

        added = 0
        for row in src.execute("SELECT * FROM assignments ORDER BY id"):
            try:
                knowledge_ids = [_mapped(x, kp_map) for x in json.loads(row["knowledge_ids"] or "[]")]
            except (json.JSONDecodeError, TypeError):
                knowledge_ids = []
            overrides = {"knowledge_ids": json.dumps([x for x in knowledge_ids if x])}
            fields = {"ta_username": row["ta_username"], "course_id": row["course_id"],
                      "title": row["title"], "class_name": row["class_name"],
                      "created_at": row["created_at"]}
            if not _find(dst, "assignments", fields):
                _insert(dst, "assignments", row, overrides=overrides)
                added += 1
        stat["assignments_added"] = added

        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        stat["integrity"] = integrity
        if integrity != "ok":
            raise RuntimeError(f"合并后完整性检查失败：{integrity}")
        if apply:
            dst.commit()
        else:
            dst.rollback()
        return stat
    except Exception:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="非破坏性合并两份 CourseLoop 业务库")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(merge(args.source, args.dest, apply=args.apply), ensure_ascii=False, indent=2))
    if not args.apply:
        print("dry-run：事务已回滚；加 --apply 才会提交。")


if __name__ == "__main__":
    main()
