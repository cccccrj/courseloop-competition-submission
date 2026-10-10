"""从旧 CourseLoop 业务库构建可重复的全功能演示库。

默认只报告；显式 ``--apply`` 才会备份目标库、复制源库并写入 showcase_* 数据。
源库始终只读，既有用户与学习记录不会被改写。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import demo_roster  # noqa: E402
from backend.learning.error_taxonomy import follow_up_of, name_of  # noqa: E402


SHOWCASE_USERS = demo_roster.USERNAMES
ERROR_CODES = [
    "knowledge_gap", "concept_confuse", "misread", "strategy", "reasoning",
    "complexity", "boundary", "calculation", "incomplete", "careless",
]
# 除 careless 外的九类具体错因：演示错题按它们轮转，careless 单独按举证门槛给
SPECIFIC_CODES = [c for c in ERROR_CODES if c != "careless"]
COURSE_WEIGHTS = ["ds-algo"] * 6 + ["co"] * 4 + ["os"] * 3 + ["net"] * 3


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _counts(path: Path) -> dict:
    tables = ("users", "problems", "knowledge_points", "practice_attempts", "error_records",
              "mastery_records", "mastery_snapshots", "assignments")
    if not path.exists():
        return {"exists": False}
    with _connect(path) as conn:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {"exists": True, **{t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                  if t in existing else None for t in tables}}


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp",
                                    dir=destination.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(tmp) as dst:
            src.backup(dst)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def restore_database(source: Path, destination: Path) -> Path | None:
    backup = None
    if destination.exists():
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = destination.with_name(f"{destination.stem}.before-demo-{stamp}{destination.suffix}")
        _sqlite_backup(destination, backup)
    _sqlite_backup(source, destination)
    return backup


def _clear_showcase(conn: sqlite3.Connection) -> None:
    holes = ",".join("?" * len(SHOWCASE_USERS))
    session_ids = [r[0] for r in conn.execute(
        f"SELECT id FROM learning_sessions WHERE username IN ({holes})", SHOWCASE_USERS)]
    if session_ids:
        sholes = ",".join("?" * len(session_ids))
        conn.execute(f"DELETE FROM dialog_turns WHERE session_id IN ({sholes})", session_ids)
    plan_ids = [r[0] for r in conn.execute(
        f"SELECT id FROM review_plans WHERE username IN ({holes})", SHOWCASE_USERS)]
    if plan_ids:
        pholes = ",".join("?" * len(plan_ids))
        task_ids = [r[0] for r in conn.execute(
            f"SELECT id FROM review_tasks WHERE plan_id IN ({pholes})", plan_ids)]
        if task_ids:
            tholes = ",".join("?" * len(task_ids))
            conn.execute(f"DELETE FROM review_task_attempts WHERE task_id IN ({tholes})", task_ids)
        conn.execute(f"DELETE FROM review_tasks WHERE plan_id IN ({pholes})", plan_ids)
    for table in ("learning_sessions", "practice_attempts", "error_records", "mastery_records",
                  "mastery_snapshots", "review_plans"):
        conn.execute(f"DELETE FROM {table} WHERE username IN ({holes})", SHOWCASE_USERS)
    conn.execute(f"DELETE FROM users WHERE username IN ({holes})", SHOWCASE_USERS)
    conn.execute("DELETE FROM assignments WHERE title LIKE '[演示]%'")


def _problem_pool(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT p.id AS problem_id,p.course_id,p.difficulty,p.stem,"
        "COALESCE((SELECT pk.knowledge_id FROM problem_knowledge pk "
        "WHERE pk.problem_id=p.id ORDER BY pk.weight DESC LIMIT 1),0) AS knowledge_id, "
        "COALESCE((SELECT k.name FROM problem_knowledge pk JOIN knowledge_points k ON k.id=pk.knowledge_id "
        "WHERE pk.problem_id=p.id ORDER BY pk.weight DESC LIMIT 1),'') AS knowledge_name "
        "FROM problems p ORDER BY p.course_id,p.id").fetchall()
    pools: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["knowledge_id"]:
            pools[row["course_id"]].append(dict(row))
    return pools


def _state(mastery: float) -> str:
    if mastery >= 0.78:
        return "mastered"
    if mastery >= 0.45:
        return "learning"
    if mastery >= 0.20:
        return "misconception"
    return "new"


def _stamp(day: date, rng: random.Random) -> str:
    return f"{day.isoformat()} {rng.randint(8, 22):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"


def seed_showcase(db_path: Path, seed: int = 4082026) -> dict:
    # import 延后到 CL_DB_PATH 生效之后，确保 db.init_db/注册写入目标库。
    os.environ["CL_DB_PATH"] = str(db_path)
    from backend.core import db

    db.init_db()
    rng = random.Random(seed)
    today = date.today()
    start = today - timedelta(days=55)

    with _connect(db_path) as conn:
        _clear_showcase(conn)
        conn.commit()

    # 姓名、学号、班级走 demo_roster —— 教务辅导员看过之后提的要求：
    # 演示数据要用学校的真实学号格式和像样的姓名，否则他判断不了
    # "这系统将来装我们班的数据是什么样"。理由与格式说明见那个模块。
    for row in demo_roster.roster():
        ok, message = db.register_user(
            row["username"], "123456", name=row["name"], role="student",
            student_id=row["student_id"], college=row["college"],
            major=row["major"], class_name=row["class_name"],
        )
        if not ok:
            raise RuntimeError(f"创建 {row['username']} 失败：{message}")

    with _connect(db_path) as conn:
        pools = _problem_pool(conn)
        missing = [c for c in {"ds-algo", "co", "os", "net"} if not pools.get(c)]
        if missing:
            raise RuntimeError(f"题库缺少课程：{missing}")

        attempts_by_user_kp: dict[tuple[str, int], list[tuple[float, str]]] = defaultdict(list)
        errors: list[int] = []
        for uidx, username in enumerate(SHOWCASE_USERS):
            ability = 0.48 + uidx * 0.035
            # 66 次覆盖前七周，另 6 次固定在最近六天：保证每位学生都进入本周榜，
            # 主展示账号也能稳定演示连续学习，而不是被随机日期碰运气。
            day_entries = [
                (start + timedelta(days=(i * 47 + uidx * 3) % 49), None)
                for i in range(66)
            ]
            day_entries.extend((today - timedelta(days=i), "ds-algo") for i in range(6))
            for seq, (day, forced_course) in enumerate(sorted(day_entries)):
                course = forced_course or COURSE_WEIGHTS[(seq + uidx) % len(COURSE_WEIGHTS)]
                item = pools[course][(seq * 11 + uidx * 17) % len(pools[course])]
                difficulty = int(item["difficulty"] or 2)
                probability = max(0.22, min(0.93, ability - (difficulty - 2.5) * 0.08))
                correct = rng.random() < probability
                score = 100.0 if correct else 0.0
                hints = 1 if rng.random() < (0.12 + (0 if correct else 0.18)) else 0
                created_at = _stamp(day, rng)
                cur = conn.execute(
                    "INSERT INTO practice_attempts(username,problem_id,answer,score,hints_used,duration,"
                    "created_at,knowledge_id,session_id) VALUES(?,?,?,?,?,?,?,?,0)",
                    (username, item["problem_id"], "演示作答", score, hints,
                     rng.randint(70, 620), created_at, item["knowledge_id"]),
                )
                attempts_by_user_kp[(username, item["knowledge_id"])].append((score, created_at))
                if not correct and rng.random() < 0.72:
                    # careless 有举证门槛（该生在这个知识点上做对过，才谈得上"手滑"），
                    # 演示数据也守这条：原先十类平均轮转，"粗心失误"稳居班级共性错因前五，
                    # 和门槛的立意正好相反（docs/34 §2.5）。
                    prior = attempts_by_user_kp[(username, item["knowledge_id"])][:-1]
                    if any(sc >= 60 for sc, _ in prior) and rng.random() < 0.15:
                        code = "careless"
                    else:
                        code = SPECIFIC_CODES[(seq + uidx * 2) % len(SPECIFIC_CODES)]
                    resolved = 1 if rng.random() < 0.42 else 0
                    resolved_at = None
                    if resolved:
                        resolved_day = min(today, day + timedelta(days=rng.randint(1, 9)))
                        resolved_at = _stamp(resolved_day, rng)
                    # 诊断用中文、说人话。原先是"演示诊断：{code}，……"，英文内部代码
                    # 直接出现在助教复核队列和学生错题本里（docs/34 §2.2）。
                    kp_name = item.get("knowledge_name") or "本题知识点"
                    diagnosis = (f"首个错误出在「{kp_name}」相关的步骤，判为{name_of(code)}。"
                                 f"建议：{follow_up_of(code)}。")
                    stem = item.get("stem") or ""
                    er = conn.execute(
                        "INSERT INTO error_records(username,problem_id,knowledge_id,error_type,diagnosis,"
                        "resolved,created_at,resolved_at,problem_stem,review_reason) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (username, item["problem_id"], item["knowledge_id"], code,
                         f"{stem[:80]} || {diagnosis}" if stem else diagnosis,
                         resolved, created_at, resolved_at, stem,
                         "careless" if code == "careless" else "manual"),
                    )
                    errors.append(er.lastrowid)

        # 当前掌握度 + 周初历史快照：周报有真实可比基线。
        for (username, knowledge_id), rows in attempts_by_user_kp.items():
            accuracy = sum(score >= 60 for score, _ in rows) / len(rows)
            mastery = round(max(0.12, min(0.94, 0.18 + accuracy * 0.72 + math.log1p(len(rows)) * 0.025)), 4)
            last = max(ts for _, ts in rows)
            state = _state(mastery)
            conn.execute(
                "INSERT INTO mastery_records(username,knowledge_id,mastery,confidence,state,last_practiced_at) "
                "VALUES(?,?,?,?,?,?)",
                (username, knowledge_id, mastery, min(0.92, 0.35 + len(rows) * 0.08), state, last),
            )
            for weeks_ago in range(5, -1, -1):
                snap_day = today - timedelta(days=today.weekday() + weeks_ago * 7 + 1)
                progress = (5 - weeks_ago) / 5
                value = round(max(0.08, mastery * (0.58 + progress * 0.42)), 4)
                conn.execute(
                    "INSERT INTO mastery_snapshots(username,knowledge_id,mastery,state,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (username, knowledge_id, value, _state(value), f"{snap_day.isoformat()} 20:00:00"),
                )

        # 人在环复核样例：一半确认，一半改判，供助教端与偏好数据脚本展示。
        for idx, error_id in enumerate(errors[:8]):
            verdict = "confirm" if idx % 2 == 0 else "override"
            original = conn.execute("SELECT error_type FROM error_records WHERE id=?", (error_id,)).fetchone()[0]
            updated = original if verdict == "confirm" else SPECIFIC_CODES[(idx + 4) % len(SPECIFIC_CODES)]
            conn.execute(
                "UPDATE error_records SET ta_reviewed=1,ta_verdict=?,ta_note=?,ta_username='demo_ta',"
                "ta_reviewed_at=?,ai_error_type=?,error_type=? WHERE id=?",
                (verdict, "演示复核：已核对首错步骤。", _stamp(today - timedelta(days=idx % 5), rng),
                 original if verdict == "override" else "", updated, error_id),
            )

        # 三份作业，完成度直接由上面的真实作答聚合得出。
        #
        # **知识点要从"学生真做过的"里挑，不能按 id 取前 9 个。** 原先取的是
        # `ORDER BY id LIMIT 9`，而上面那段作答生成器铺在掌握度模型碰过的点上 ——
        # 两者几乎不重叠，结果三份作业的完成率全是 0%。演示时一张"完成 0/6"的
        # 作业卡，看的人只会以为这个功能没做完，而不会想到是造数据挑错了点。
        def _hot_points(klass: str, want: int) -> list[int]:
            """这个班真做过、且做对最多的几个知识点。

            **必须按班取，不能按全体取。** 作业是发给某一个班的，而按全体排出来的
            热门点未必是这个班练过的 —— 实测按全体挑时，发给计科2202 的那份完成率
            是 0%，因为排在前面的点都是另一个班在做。
            """
            sql = ("SELECT a.knowledge_id FROM practice_attempts a "
                   "JOIN knowledge_points k ON k.id = a.knowledge_id "
                   "JOIN users u ON u.username = a.username "
                   "WHERE k.course_id='ds-algo' AND a.score >= 60 ")
            params: list = []
            if klass:
                sql += "AND COALESCE(u.class_name,'') = ? "
                params.append(klass)
            sql += "GROUP BY a.knowledge_id ORDER BY COUNT(*) DESC, a.knowledge_id LIMIT ?"
            params.append(want)
            got = [r[0] for r in conn.execute(sql, params)]
            if len(got) < want:     # 兜底：宁可完成率低，也不要建不出作业
                got += [r[0] for r in conn.execute(
                    "SELECT id FROM knowledge_points WHERE course_id='ds-algo' "
                    "ORDER BY id LIMIT ?", (want,)) if r[0] not in got][:want - len(got)]
            return got
        # 前两条按班下发、第三条面向全体；班名必须与花名册一致，
        # 对不上的话学生那边一条作业都收不到，而且不会有任何报错。
        for idx, class_name in enumerate((demo_roster.class_name(1),
                                          demo_roster.class_name(2), "")):
            # 每份作业 2 个点、每点 1 道。参数是按造出来的作答实测挑的：
            # 生成器给每个知识点造的是 1~2 道正确作答，3 个点 × 每点 2 道时
            # 12 个人里 0 个能完成，演示出来就是一排 0%；2 个点 × 每点 1 道，
            # 完成的与没完成的能在进度条上分布开 —— 这才像一个真实的班。
            ids = _hot_points(class_name, 2)
            conn.execute(
                "INSERT INTO assignments(ta_username,course_id,title,note,knowledge_ids,target_count,"
                "class_name,due_date,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("demo_ta", "ds-algo", f"[演示] 第{idx + 1}阶段巩固", "根据薄弱点完成三道不同题目。",
                 json.dumps(ids), 1, class_name, (today + timedelta(days=7 + idx)).isoformat(),
                 "open", f"{(today - timedelta(days=10 - idx)).isoformat()} 09:00:00"),
            )

        # 主展示账号的复习计划与任务。
        focal = SHOWCASE_USERS[0]
        focal_points = [r[0] for r in conn.execute(
            "SELECT knowledge_id FROM mastery_records WHERE username=? ORDER BY mastery LIMIT 8", (focal,))]
        plan = conn.execute(
            "INSERT INTO review_plans(username,course_id,goal,start_date,exam_date,status,daily_minutes,"
            "created_at,summary,span_days,baseline_mastery,baseline_open_errors) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (focal, "ds-algo", "两周内补齐高频薄弱点", today.isoformat(),
             (today + timedelta(days=13)).isoformat(), "active", 60,
             f"{today.isoformat()} 07:30:00", "演示复习计划", 14, 0.42, 6),
        ).lastrowid
        for idx, kid in enumerate(focal_points):
            task_day = today + timedelta(days=idx)
            conn.execute(
                "INSERT INTO review_tasks(plan_id,knowledge_id,task_date,task_type,target_count,completed,"
                "day_index,review_round,stage,interval_days,minutes,reason,priority,error_type,"
                "completed_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan, kid, task_day.isoformat(), "practice", 3, 1 if idx < 2 else 0,
                 idx, 1, 0, 1, 20, "掌握度偏低且近期有错题", 1.0 - idx * 0.05,
                 SPECIFIC_CODES[idx % len(SPECIFIC_CODES)], 3 if idx < 2 else 0,
                 f"{today.isoformat()} 07:30:00"),
            )

        # 三段历史对话，让记录页与 TutorGraph 轨迹不为空。
        for idx in range(3):
            sid = conn.execute(
                "INSERT INTO learning_sessions(username,course_id,problem_id,intent,state,started_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (focal, "ds-algo", 0, "chat", "completed",
                 _stamp(today - timedelta(days=idx + 1), rng), _stamp(today - timedelta(days=idx + 1), rng)),
            ).lastrowid
            conn.executemany(
                "INSERT INTO dialog_turns(session_id,role,content,structured_data,created_at) VALUES(?,?,?,?,?)",
                [(sid, "student", "请讲解这个知识点的核心思路。", "", _stamp(today - timedelta(days=idx + 1), rng)),
                 (sid, "assistant", "先明确定义，再用一个可验证的小例子逐步推导。", "{}",
                  _stamp(today - timedelta(days=idx + 1), rng))],
            )
        conn.commit()

    return _counts(db_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="旧业务库 → 全功能 CourseLoop 演示库（两个阶段可分别执行）",
        epilog="不加 --restore-only / --seed-only 时两个阶段都跑（等同旧行为）。")
    parser.add_argument("--source-db", type=Path,
                        help="旧业务库；只有 restore 阶段需要它")
    parser.add_argument("--dest-db", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=4082026)
    parser.add_argument("--apply", action="store_true")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--restore-only", action="store_true",
                       help="只用源库覆盖目标库，不造演示数据")
    phase.add_argument("--seed-only", action="store_true",
                       help="只对现有目标库造 12 个 showcase 学生，不碰题库")
    args = parser.parse_args()

    # 两个阶段拆开，是因为它们在重建流程里的**位置不同**：restore 会整库覆盖（题库
    # 一起换掉），必须最先跑；seed 造的作答记录引用 problems.id，必须最后跑。
    # 绑在一起的话，"我只想补一批作答"就得连带把刚灌好的题库覆盖掉。
    do_restore = not args.seed_only
    do_seed = not args.restore_only
    if do_restore and args.source_db is None:
        parser.error("restore 阶段需要 --source-db（只想造演示数据请加 --seed-only）")
    if args.seed_only and args.source_db is not None:
        # 宁可报错也不静默忽略：--seed-only 不覆盖任何东西，而传了源库的人
        # 多半以为它会。让这个误解在写库之前暴露出来。
        parser.error("--seed-only 不会读源库，请去掉 --source-db")

    summary = {"destination": _counts(args.dest_db)}
    if args.source_db is not None:
        summary["source"] = _counts(args.source_db)
    summary["phases"] = [n for n, on in (("restore", do_restore), ("seed", do_seed)) if on]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if do_restore:
        print("⚠ restore 会用源库整个覆盖目标库，**题库也会被换掉**。"
              "若目标库里已经灌好题，请改用 --seed-only。")
    if not args.apply:
        print("dry-run：未写入。加 --apply 执行。")
        return

    backup = restore_database(args.source_db, args.dest_db) if do_restore else None
    result = seed_showcase(args.dest_db, args.seed) if do_seed else _counts(args.dest_db)
    print(json.dumps({"backup": str(backup) if backup else None, "result": result},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
