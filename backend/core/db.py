"""CourseLoop 的 SQLite 数据层。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from backend.core.config import settings
from backend.core.security import normalize_role


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_TRANSACTION: ContextVar[tuple[str, sqlite3.Connection] | None] = ContextVar("courseloop_transaction", default=None)
_CONNECTION_SCOPE: ContextVar[tuple[str, sqlite3.Connection] | None] = ContextVar("courseloop_connection_scope", default=None)
_WRITE_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_WRITE_LOCK_GUARD = threading.Lock()
_CONNECTION_LIMITS: dict[tuple[int, str], threading.BoundedSemaphore] = {}


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _owned_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_conn():
    active = _TRANSACTION.get()
    if active is not None and active[0] == settings.db_path:
        # 原子业务操作内所有既有helper复用连接，不在中间偷偷提交。
        yield active[1]
        return
    scoped = _CONNECTION_SCOPE.get()
    if scoped is not None and scoped[0] == settings.db_path:
        # 仅复用同步请求连接；普通helper仍保持原有提交/回滚边界。
        # 与原子transaction不同，这里不能悄悄把多步操作变成长事务。
        try:
            yield scoped[1]
            scoped[1].commit()
        except BaseException:
            scoped[1].rollback()
            raise
        return
    # 同一SQLite库的短查询限制并行连接数，避免40个HTTP工作线程同时
    # 建连接/编译SQL争抢CPU，拖长唯一写者事务并造成写入队列积压。
    # 不缓存用户数据、不改变事务隔离或提交持久性；不同进程由SQLite协调。
    key = (os.getpid(), os.path.realpath(settings.db_path))
    with _WRITE_LOCK_GUARD:
        slots = _CONNECTION_LIMITS.setdefault(key, threading.BoundedSemaphore(8))
    if not slots.acquire(timeout=10):
        raise sqlite3.OperationalError("connection queue timeout")
    try:
        with _owned_conn() as conn:
            yield conn
    finally:
        slots.release()


@contextmanager
def connection_scope():
    """短同步业务请求复用连接；不缓存结果，不跨线程/await共享连接。"""
    active = _TRANSACTION.get() or _CONNECTION_SCOPE.get()
    if active is not None and active[0] == settings.db_path:
        yield active[1]
        return
    with get_conn() as conn:
        token = _CONNECTION_SCOPE.set((settings.db_path,conn))
        try:
            yield conn
        finally:
            _CONNECTION_SCOPE.reset(token)


@contextmanager
def transaction():
    """同步短写事务：先取SQLite写锁，再读旧值，跨线程/进程不会丢更新。

    仅包数据库与纯计算；不能在此范围里await、请求模型或运行代码沙箱。
    WAL与5秒连接busy timeout允许其他读连接继续工作，并有界等待写锁。
    """
    active = _TRANSACTION.get()
    if active is not None and active[0] == settings.db_path:
        conn = active[1]
        conn.execute("SAVEPOINT courseloop_nested_write")
        try:
            yield conn
            conn.execute("RELEASE courseloop_nested_write")
        except BaseException:
            conn.execute("ROLLBACK TO courseloop_nested_write")
            conn.execute("RELEASE courseloop_nested_write")
            raise
        return
    # 同进程写入先排队，避免几十个线程各自在SQLite busy重试中饥饿到5秒。
    # 这不是正确性的唯一保障：独立进程仍由BEGIN IMMEDIATE数据库锁保护。
    key = (os.getpid(), os.path.realpath(settings.db_path))
    with _WRITE_LOCK_GUARD:
        write_lock = _WRITE_LOCKS.setdefault(key, threading.Lock())
    if not write_lock.acquire(timeout=10):
        raise sqlite3.OperationalError("write queue timeout")
    try:
        # 独立写者通道：取到写锁后不再争用读连接名额，防止读队列饿死写者。
        # 每进程最多8个普通helper连接+1个原子写事务连接。
        with _owned_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            token = _TRANSACTION.set((settings.db_path, conn))
            try:
                yield conn
            finally:
                _TRANSACTION.reset(token)
    finally:
        write_lock.release()


def _hash_pw(pw: str, salt: str) -> str:
    return hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()


# users 在基础六列之外的注册资料列。用 ALTER 幂等补齐而不是改 CREATE TABLE ——
# 使用幂等迁移兼容已有部署，避免升级时重建用户表。
# SQLite 加列是 O(1) 元数据操作，旧行自动填 NULL。
_USER_EXTRA_COLS = {
    "student_id": "TEXT",
    "college": "TEXT",
    "major": "TEXT",      # 专业注册时自填：学院可穷举，专业太细不宜做成选项
    "class_name": "TEXT",
    "email": "TEXT",
    "phone": "TEXT",
    "avatar": "TEXT",      # 前端裁剪压缩后的 data URL
}

# 注册资料在库里与 API 上的字段名一致，写入/回读都遍历这一份清单，
# 避免"加了列忘了回读"这类两处不同步的错。
_PROFILE_FIELDS = tuple(_USER_EXTRA_COLS)

# 管理员才能改的列，**刻意不并进 _USER_EXTRA_COLS**。
# 并进去的话它就成了 _PROFILE_FIELDS 的一员，而 PUT /auth/profile 会遍历
# _PROFILE_FIELDS 写库 —— 被停用的人只要改一次自己的资料就能把自己启用回来。
_USER_ADMIN_COLS = {
    "status": "TEXT DEFAULT 'active'",   # active / disabled
}

USER_STATUSES = ("active", "disabled")


# 其他表同样使用幂等 ALTER 迁移，不重建已有数据。
_TABLE_EXTRA_COLS = {
    "problems": {
        # 出这道题时用的教材片段出处（JSON 数组，形状与 rag_context 的 citations 一致）。
        # 生成题此前只是"拿教材当参考"，出处随手丢掉了 —— 于是 2646 块语料和 2950 道题
        # 之间没有任何连接，讲评时说不出这题依据哪一页。旧题这一列是空串，读取方按
        # "没有出处"处理，不需要数据迁移。
        "citations": "TEXT DEFAULT ''",
    },
    "ingest_jobs": {
        # 撤下的时间与原路径。撤下不删文件，只把它移出 data/raw（扫描路径）——
        # 留着原路径才能原样放回去。旧任务行这两列是 NULL，读取方按"没撤下过"处理。
        "withdrawn_at": "TEXT",
        "origin_path": "TEXT DEFAULT ''",
    },
    "review_plans": {
        "daily_minutes": "INTEGER DEFAULT 60",
        "created_at": "TEXT",
        "summary": "TEXT DEFAULT ''",
        "span_days": "INTEGER DEFAULT 0",
        "baseline_mastery": "REAL DEFAULT 0",
        "baseline_open_errors": "INTEGER DEFAULT 0",
        # 以下三列服务于「按时间预算滚动排期」。旧计划三列都是 NULL，
        # 读取方一律按"没有预算、已排到 exam_date、从未 reconcile 过"处理，
        # 于是老计划继续按原来的方式工作，不需要数据迁移。
        "budget_snapshot": "TEXT DEFAULT ''",     # 生成时的预算 JSON，用于解释"为什么这么排"
        "materialized_through": "TEXT",           # 滚动窗口已详细排到哪一天
        "reconciled_on": "TEXT",                  # 最近一次自愈重排的日期，保证一天只跑一次
        "milestones": "TEXT DEFAULT ''",          # 窗口之外的周级配额 JSON
    },
    "review_tasks": {
        "day_index": "INTEGER DEFAULT 0",
        "review_round": "INTEGER DEFAULT 1",
        "stage": "INTEGER DEFAULT 0",
        "interval_days": "INTEGER DEFAULT 0",
        "minutes": "INTEGER DEFAULT 15",
        "reason": "TEXT DEFAULT ''",
        "priority": "REAL DEFAULT 0",
        "error_type": "TEXT DEFAULT ''",
        "completed_count": "INTEGER DEFAULT 0",
        "completed_at": "TEXT",
        "created_at": "TEXT",
        # tier 默认 'core'：老任务行没有分层概念，全部视作必做，
        # 与改动前的行为一致（那时每一项都是"今天必须做完"）。
        "tier": "TEXT DEFAULT 'core'",
        # 到期窗口。逾期判定看 due_to 而不是 task_date —— 间隔复习本来就有容差，
        # 晚一天做不该被叫作逾期。老行两列为空，读取方回落到 task_date。
        "due_from": "TEXT",
        "due_to": "TEXT",
        # 债务超限时被放弃的轮次。不删行：放弃了什么、什么时候放弃的要留痕。
        "dropped": "INTEGER DEFAULT 0",
    },
    "review_task_attempts": {
        "correct": "INTEGER DEFAULT 0",
        "hints_used": "INTEGER DEFAULT 0",
    },
    # TutorGraph（智能解题页）的作答对着自由输入的题目，没有 problems.id 可挂，
    # 于是 save_attempt 一直按 problem_id=0 落库 —— 而 _KNOWLEDGE_STATS_SQL 的
    # att 子查询有 `a.problem_id > 0` 过滤，这些作答统统进不了薄弱画像。
    # 补一列直接记知识点：题目可以没有编号，知识点是一定知道的。
    # session_id 同时留下会话线索，让一次作答能回溯到当时的完整对话。
    "practice_attempts": {
        "knowledge_id": "INTEGER DEFAULT 0",
        "session_id": "INTEGER DEFAULT 0",
    },
    # 间隔复习的 stage 存在 (学生, 知识点) 这一层，不存在具体某次任务上 ——
    # stage 是长期属性，必须跨计划存活，否则每次重新排计划都从 1 天重来。
    "mastery_records": {
        "review_stage": "INTEGER DEFAULT 0",
        "next_due_at": "TEXT",
        "last_reviewed_at": "TEXT",
    },
    # 助教复核/改判：ai_error_type 只在真被改判时才写入，留原始 AI 判定的痕迹，
    # 供 doc00 说的"人在环修订"日后当偏好训练数据用；error_type 本身随改判更新，
    # 让班级共性错因聚合直接反映纠正后的结果，不用另开一套统计口径。
    "error_records": {
        "ta_reviewed": "INTEGER DEFAULT 0",
        "ta_verdict": "TEXT DEFAULT ''",
        "ta_note": "TEXT DEFAULT ''",
        "ta_username": "TEXT DEFAULT ''",
        "ta_reviewed_at": "TEXT",
        "ai_error_type": "TEXT DEFAULT ''",
        # resolved 只是个布尔，销账发生在哪天没有留痕，于是"本周销掉几道错题"
        # 这种按时间窗口的统计没有数据来源，只能拿总量冒充。补一列记时刻。
        # 旧行为 NULL：它们的销账时间确实不知道，窗口统计不认它们才是诚实的。
        "resolved_at": "TEXT",
        # 复核要看的上下文（docs/34 §3.6）。此前题干被拼进 diagnosis（"题干 || 诊断"），
        # 助教侧又把前半截切掉了；学生答案根本没存 —— 助教是在不知道题目、不知道学生
        # 写了什么的情况下改判。旧行留空，读取时从 diagnosis 的 " || " 前半截回填题干。
        "problem_stem": "TEXT DEFAULT ''",
        "student_answer": "TEXT DEFAULT ''",
        # 为什么进复核队列（docs/35 §2.2）：student_request / verify_failed / no_evidence /
        # careless / manual。不用"置信度"——诊断链路没有可信的置信度来源，
        # 入队原因则每一条都能说清楚是怎么来的。
        "review_reason": "TEXT DEFAULT 'manual'",
        "review_requested_at": "TEXT",
        # 代码题运行失败的原始技术细节（异常类名 / 原文 / 行号，I-05）。diagnosis 里只放
        # 中文说明；这一列只在助教复核卡片的"技术详情"里出现，学生端接口不下发。
        "tech_detail": "TEXT DEFAULT ''",
    },
    # 作业的来源。原先作业只说"做什么"，不说"为什么是这几个知识点" —— 学生看到的
    # 是一份凭空出现的任务，助教过两周也想不起当初为何布置。存一个错因 code 而不是
    # 一句自由文本：它能和班级共性错因聚合对上，日后要回答"针对这个错因的干预有没有
    # 效果"才有连接键。空串 = 助教手动创建，不假装有数据依据。
    "assignments": {
        "source_error_type": "TEXT DEFAULT ''",
    },
}


def _ensure_columns(cur, table: str, cols: dict[str, str]) -> None:
    existing = {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, typ in cols.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def _ensure_user_columns(cur):
    _ensure_columns(cur, "users", _USER_EXTRA_COLS)
    _ensure_columns(cur, "users", _USER_ADMIN_COLS)


def _user_status_of(row) -> str:
    """读一行用户的状态。老库里这一列可能还不存在或为 NULL —— 一律当 active。

    默认 active 而不是 disabled 是有意的：迁移时机没对上就把全体用户锁在门外，
    比多留一会儿权限严重得多，而且前者的现场表现是"所有人都登不进去"。
    """
    try:
        value = row["status"]
    except (IndexError, KeyError):
        return "active"
    return value if value in USER_STATUSES else "active"


def user_status(username: str) -> str | None:
    """按用户名取状态；用户不存在返回 None。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?",
                           ((username or "").strip(),)).fetchone()
    return _user_status_of(row) if row else None


def set_user_status(username: str, status: str) -> bool:
    if status not in USER_STATUSES:
        raise ValueError(f"未知的用户状态：{status!r}")
    with get_conn() as conn:
        cur = conn.execute("UPDATE users SET status=? WHERE username=?",
                           (status, (username or "").strip()))
        return cur.rowcount > 0


def set_user_role(username: str, role: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE users SET role=? WHERE username=?",
                           (normalize_role(role), (username or "").strip()))
        return cur.rowcount > 0


def list_users(role: str = "", status: str = "", keyword: str = "") -> list[dict]:
    """管理页的用户列表。**不返回 pw_hash / salt**，也不返回头像（体积大且无用）。"""
    sql = ["SELECT username,role,name,created_at,student_id,college,major,class_name,"
           "email,phone,status FROM users WHERE 1=1"]
    args: list[str] = []
    if role:
        sql.append("AND role=?")
        args.append(role)
    if status:
        sql.append("AND status=?")
        args.append(status)
    if keyword:
        sql.append("AND (username LIKE ? OR name LIKE ? OR student_id LIKE ?)")
        args += [f"%{keyword}%"] * 3
    sql.append("ORDER BY role, username")
    with get_conn() as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()
    return [{**{k: (r[k] or "") for k in r.keys()}, "status": _user_status_of(r)} for r in rows]


def init_db():
    """幂等建表 + 种子演示账号。"""
    with get_conn() as conn:
        # journal_mode持久化于数据库，只在初始化/迁移时设置，避免每次读请求重复切换。
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'student',
                name TEXT,
                pw_hash TEXT, salt TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS knowledge_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id TEXT, chapter TEXT, name TEXT, description TEXT, difficulty INTEGER
            );
            CREATE TABLE IF NOT EXISTS problems (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id TEXT, source TEXT, type TEXT, stem TEXT,
                difficulty INTEGER, answer TEXT, verification_type TEXT
            );
            CREATE TABLE IF NOT EXISTS learning_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, course_id TEXT, problem_id INTEGER,
                intent TEXT, state TEXT, started_at TEXT, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS dialog_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER, role TEXT, content TEXT,
                structured_data TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tutor_snapshots (
                db_session_id INTEGER PRIMARY KEY,
                session_id TEXT, username TEXT, snapshot TEXT, updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tutor_snapshots_sid
                ON tutor_snapshots(session_id);
            CREATE TABLE IF NOT EXISTS practice_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, problem_id INTEGER, answer TEXT,
                score REAL, hints_used INTEGER, duration INTEGER, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS error_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, problem_id INTEGER, knowledge_id INTEGER,
                error_type TEXT, diagnosis TEXT, resolved INTEGER DEFAULT 0, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS mastery_records (
                username TEXT, knowledge_id INTEGER,
                mastery REAL, confidence REAL, state TEXT, last_practiced_at TEXT,
                PRIMARY KEY (username, knowledge_id)
            );
            CREATE TABLE IF NOT EXISTS test_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id INTEGER, input TEXT, expected_output TEXT, hidden INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS rubric_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id INTEGER, description TEXT, score REAL, expected_evidence TEXT
            );
            CREATE TABLE IF NOT EXISTS problem_knowledge (
                problem_id INTEGER, knowledge_id INTEGER, weight REAL DEFAULT 1.0
            );
            CREATE TABLE IF NOT EXISTS review_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, course_id TEXT, goal TEXT, start_date TEXT, exam_date TEXT, status TEXT
            );
            CREATE TABLE IF NOT EXISTS review_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER, knowledge_id INTEGER, task_date TEXT,
                task_type TEXT, target_count INTEGER, completed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS review_task_attempts (
                task_id INTEGER, problem_id INTEGER, correct INTEGER DEFAULT 0,
                hints_used INTEGER DEFAULT 0, created_at TEXT,
                PRIMARY KEY (task_id, problem_id)
            );
            -- mastery_records 只存"此刻的掌握度"，被覆盖写，没有任何历史。
            -- 学情周报要回答"这周涨了多少"，而这个问题在没有历史的库上无解 ——
            -- 与其拿计划基线（review_plans.baseline_mastery，那是计划开始时的快照，
            -- 不是周初）冒充周初值，不如把每次写入都留一行。追加写，不更新不删除。
            CREATE TABLE IF NOT EXISTS mastery_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, knowledge_id INTEGER,
                mastery REAL, state TEXT, created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mastery_snapshots_lookup
                ON mastery_snapshots(username, knowledge_id, created_at);
            -- 助教下发的作业。刻意**不建"每个学生一行"的领取表**：验收完全由
            -- practice_attempts 算出来（下发之后、这些知识点上、答对几道），
            -- 学生那边不需要"接受作业"这个动作，做题本身就是完成作业。
            -- 多一张状态表就多一处要跟作答对账的地方，而那两份数据迟早会不一致。
            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ta_username TEXT, course_id TEXT, title TEXT, note TEXT,
                knowledge_ids TEXT,          -- JSON 数组
                target_count INTEGER DEFAULT 3,
                class_name TEXT DEFAULT '',  -- 空串 = 面向全体
                due_date TEXT, status TEXT DEFAULT 'open', created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_assignments_lookup
                ON assignments(course_id, status, class_name);
            -- 备考时间预算。刻意按**人**存一行而不是 (人, 课)：一个人一天只有那么多
            -- 时间，四门课共享它。若每门各存一份，四份计划会各自认为自己拥有全部预算，
            -- 加起来远超真实可投入时间 —— 而这种超卖在界面上完全看不出来。
            -- 分摊到具体某门课由 study_budget.course_share() 按 408 分值权重算。
            CREATE TABLE IF NOT EXISTS study_budgets (
                username TEXT PRIMARY KEY,
                weekly_minutes TEXT,                -- JSON 数组，7 个整数，索引 0=周一 … 6=周日
                intensity TEXT DEFAULT 'medium',    -- light / medium / sprint，决定填充率
                buffer_weekday INTEGER DEFAULT 6,   -- 每周机动日 0..6；-1 = 不留
                exam_date TEXT,                     -- 初试日期，四门共用
                updated_at TEXT
            );
            -- 例外日期：实习、回家、期末周这类"整段没时间"，周模板表达不了。
            -- 覆盖式语义（存在即以此为准，包括 0 分钟），整体替换而非增量合并。
            CREATE TABLE IF NOT EXISTS study_budget_exceptions (
                username TEXT, date TEXT, minutes INTEGER, note TEXT DEFAULT '',
                PRIMARY KEY (username, date)
            );
            -- 课程注册表。原先是 backend/core/courses.py 里的 Python 字面量，
            -- 于是"开一门新课"＝改代码+重启。搬进库之后教务可以自助开课。
            -- 四个 JSON 列存的是天然嵌套的结构（知识点树、错因示例、示例问题），
            -- 拆成关联表除了让每次读课程都要 join 四张表之外没有任何收益 ——
            -- 它们从来不被单独查询，只被整体读出来组装 Course 对象。
            CREATE TABLE IF NOT EXISTS courses (
                id TEXT PRIMARY KEY,
                display TEXT NOT NULL, short TEXT NOT NULL, exam TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                code_enabled INTEGER DEFAULT 0,
                verification_types TEXT DEFAULT '[]',
                knowledge_taxonomy TEXT DEFAULT '[]',
                error_examples TEXT DEFAULT '{}',
                sample_questions TEXT DEFAULT '[]',
                answer_format TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1
            );
            -- 资料入库任务。教务在管理页传一份课件，这里记一行，后台解析完回填。
            -- **失败要留痕**：解析失败的文件如果只是不出现在列表里，教务会以为
            -- 传丢了而反复重传，而真正的原因（扫描件没有文本层、文件损坏）
            -- 一次都没被看见。所以 status 有 failed 这一档，error 原样存。
            CREATE TABLE IF NOT EXISTS ingest_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT, stored_path TEXT,
                course_id TEXT, doc_type TEXT, parser TEXT,
                status TEXT DEFAULT 'pending',      -- pending/running/done/failed/withdrawn
                error TEXT DEFAULT '',
                parsed_chunks INTEGER DEFAULT 0,    -- 解析出多少块
                indexed_chunks INTEGER DEFAULT 0,   -- 真正写进索引的块（去重后）
                duplicate_chunks INTEGER DEFAULT 0, -- 因内容重复被跳过的块
                size_bytes INTEGER DEFAULT 0,
                uploaded_by TEXT, created_at TEXT, finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ingest_jobs_created ON ingest_jobs(created_at DESC);
            -- 全局设置。目前只有 exam_structure（试卷结构描述）——
            -- 它原是写死 408 的模块常量，开了 408 之外的课之后那句话就不对了。
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY, value TEXT DEFAULT '', updated_at TEXT
            );
            -- 学生对 AI 问答的复核申请（docs/35 P1-2）。解题 / 拍照产生的是错题，
            -- 申请复核直接标在 error_records 上；大厅问答不是错题，塞进错题表会被算进
            -- "待解决错题"和共性错因，所以单独一张表。
            CREATE TABLE IF NOT EXISTS review_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL, course_id TEXT DEFAULT '',
                source TEXT DEFAULT 'hall',          -- hall / solve / photo
                question TEXT DEFAULT '', ai_answer TEXT DEFAULT '', note TEXT DEFAULT '',
                no_evidence INTEGER DEFAULT 0,       -- 这条 AI 回答当时有没有检索到教材依据
                status TEXT DEFAULT 'open',          -- open / answered
                ta_username TEXT DEFAULT '', ta_verdict TEXT DEFAULT '', ta_reply TEXT DEFAULT '',
                created_at TEXT, answered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_review_requests_status ON review_requests(status, id);
            """
        )
        # app_settings 曾短暂叫过 tenant_settings。空的就顺手删掉，
        # 留一张没人引用的同义表只会让下一个人怀疑自己看漏了什么。
        if cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tenant_settings'"
                       ).fetchone() and not cur.execute(
                           "SELECT 1 FROM tenant_settings LIMIT 1").fetchone():
            cur.execute("DROP TABLE tenant_settings")

        _ensure_user_columns(cur)
        for table, cols in _TABLE_EXTRA_COLS.items():
            _ensure_columns(cur, table, cols)

        # problems 扩展列（func_name/ref_code/knowledge/hint）——幂等迁移
        # hint：出题 agent 本来就会产出一级提示（不泄露答案），原先入库时丢掉了，
        # 于是题库做题没有任何提示可用，hints_used 永远是 0，掌握度模型的提示惩罚
        # 与间隔复习的「依赖提示不进阶」两条分支都成了死代码。
        cols = {r[1] for r in cur.execute("PRAGMA table_info(problems)").fetchall()}
        for col in ("func_name", "ref_code", "knowledge", "hint"):
            if col not in cols:
                cur.execute(f"ALTER TABLE problems ADD COLUMN {col} TEXT DEFAULT ''")

        # 只新增索引，不重建或删数据。按学生隔离的热查询避免扫描全部人的行为记录。
        cur.executescript("""
            CREATE INDEX IF NOT EXISTS idx_attempts_user_knowledge ON practice_attempts(username,knowledge_id,id);
            CREATE INDEX IF NOT EXISTS idx_attempts_user_problem ON practice_attempts(username,problem_id,id);
            -- 不按题号/知识点分组的最近记录另用用户+id；有用时样本使用覆盖部分索引，
            -- 避免大量duration=0的压测/历史记录参与排序与回表。
            CREATE INDEX IF NOT EXISTS idx_attempts_user_recent ON practice_attempts(username,id);
            CREATE INDEX IF NOT EXISTS idx_attempts_user_duration ON practice_attempts(username,id,duration) WHERE duration>0;
            CREATE INDEX IF NOT EXISTS idx_errors_user_knowledge ON error_records(username,knowledge_id,resolved,error_type);
            CREATE INDEX IF NOT EXISTS idx_problems_course_knowledge ON problems(course_id,knowledge,id);
            CREATE INDEX IF NOT EXISTS idx_knowledge_course_name ON knowledge_points(course_id,name);
            CREATE INDEX IF NOT EXISTS idx_problem_knowledge_problem ON problem_knowledge(problem_id,knowledge_id);
            CREATE INDEX IF NOT EXISTS idx_problem_knowledge_knowledge ON problem_knowledge(knowledge_id,problem_id);
            CREATE INDEX IF NOT EXISTS idx_test_cases_problem ON test_cases(problem_id);
            CREATE INDEX IF NOT EXISTS idx_rubric_problem ON rubric_items(problem_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user_course ON learning_sessions(username,course_id,intent,id);
            CREATE INDEX IF NOT EXISTS idx_dialog_session ON dialog_turns(session_id,id);
        """)

        # 种子课程（幂等）。只在表**整体为空**时播种，不逐门补 ——
        # 删掉某门课是个明确动作，逐门 INSERT OR IGNORE 会让它下次重启又长回来。
        if not cur.execute("SELECT 1 FROM courses LIMIT 1").fetchone():
            from backend.core.courses import DEFAULT_SEED, _to_row
            cur.executemany(
                "INSERT INTO courses(id,display,short,exam,score,code_enabled,"
                "verification_types,knowledge_taxonomy,error_examples,sample_questions,"
                "answer_format,sort_order,enabled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [_to_row(c, i) for i, c in enumerate(DEFAULT_SEED)])

        # 种子演示账号（幂等）
        for uname, role, name in [("demo_student", "student", "演示学生"),
                                  ("demo_ta", "ta", "演示助教")]:
            row = cur.execute("SELECT 1 FROM users WHERE username=?", (uname,)).fetchone()
            if not row:
                salt = os.urandom(8).hex()
                cur.execute(
                    "INSERT INTO users(username,role,name,pw_hash,salt,created_at) VALUES(?,?,?,?,?,?)",
                    (uname, role, name, _hash_pw("123456", salt), salt, _now()),
                )


def register_user(username: str, password: str, name: str = "", role: str = "student",
                  **profile: str) -> tuple[bool, str]:
    """自助注册。username 唯一；学号若填则同样查重（一人一号，防重复注册刷数据）。

    profile 接收 _PROFILE_FIELDS 中的注册资料（学号/学院/专业/班级/邮箱/手机），
    未知键直接忽略 —— 这些值来自公网表单，不该让它们决定拼进 SQL 的列名。

    role 供批量建号脚本指定助教/管理员账号；`/auth/register` 端点固定不传，
    公网注册一律落成 student。取值域见 security.ROLES —— 那是唯一一份词表，
    不在词表里的值静默降级为 student（这个函数也服务公网注册路径）。
    """
    username = (username or "").strip()
    if not username or not password:
        return False, "用户名和密码不能为空"
    fields = {k: (profile.get(k) or "").strip() for k in _PROFILE_FIELDS}
    with get_conn() as conn:
        cur = conn.cursor()
        if cur.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return False, "用户名已存在"
        if fields["student_id"] and cur.execute(
                "SELECT 1 FROM users WHERE student_id=?", (fields["student_id"],)).fetchone():
            return False, "该学号已注册过，请勿重复注册"
        salt = os.urandom(8).hex()
        cols = ",".join(_PROFILE_FIELDS)
        marks = ",".join("?" * len(_PROFILE_FIELDS))
        cur.execute(
            f"INSERT INTO users(username,role,name,pw_hash,salt,created_at,{cols}) "
            f"VALUES(?,?,?,?,?,?,{marks})",
            (username, normalize_role(role),
             name or username, _hash_pw(password, salt), salt, _now(),
             *(fields[k] for k in _PROFILE_FIELDS)),
        )
    return True, "注册成功"


def _user_payload(row: sqlite3.Row) -> dict:
    """登录/注册返回给前端的用户对象。注册后自动登录与随后重新登录必须给出
    同一形状 —— 否则 localStorage 里的身份会因为登录方式不同而字段忽有忽无。"""
    keys = row.keys()
    out = {"username": row["username"], "role": row["role"], "name": row["name"]}
    out.update({k: (row[k] or "") for k in _PROFILE_FIELDS if k in keys})
    return out


def verify_user(username: str, password: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
    if not row:
        return None
    if _hash_pw(password, row["salt"]) != row["pw_hash"]:
        return None
    # 已停用的账号连登录都不给过。口令对不对与账号能不能用是两件事，
    # 但对外只报同一句"用户名或密码错误"由端点决定 —— 这里只负责如实返回 None。
    if _user_status_of(row) != "active":
        return None
    return _user_payload(row)


def get_user_profile(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return _user_payload(row) if row else None


def update_user_profile(username: str, **values: str) -> dict | None:
    """只允许更新展示资料；用户名、学号、角色与密码均不可由此接口修改。"""
    allowed = ("name", "college", "major", "class_name", "email", "phone", "avatar")
    clean = {key: str(values.get(key, "") or "").strip() for key in allowed}
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET " + ",".join(f"{key}=?" for key in allowed) + " WHERE username=?",
            (*(clean[key] for key in allowed), username),
        )
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return _user_payload(row) if row else None


def set_user_class(username: str, class_name: str) -> bool:
    """只改班级，别的资料一个都不动。

    **不能复用 `update_user_profile`**：那个函数是给"我改我自己的资料"用的，
    它把 allowed 里每一列都写一遍，没传的写空串。教务批量改班时若走它，
    学生的姓名、学院、专业、邮箱、手机会被一次性抹掉 —— 而且不会有任何报错。
    """
    class_name = (class_name or "").strip()[:80]
    with get_conn() as conn:
        cur = conn.execute("UPDATE users SET class_name=? WHERE username=?",
                           (class_name, username))
        return cur.rowcount > 0


REVIEW_REASONS = ("student_request", "verify_failed", "no_evidence", "careless", "manual")


def save_error_record(username: str, error_type: str, diagnosis: str,
                      knowledge: str = "", problem_stem: str = "",
                      problem_id: int = 0, knowledge_id: int = 0,
                      student_answer: str = "", review_reason: str = "manual",
                      tech_detail: str = "") -> int:
    """写一条错题。

    `diagnosis` 仍沿用"题干前 80 字 || 诊断"的老格式 —— 错题本、周报、偏好数据脚本
    等六处读取方都按它切分，改格式等于同时改六处。完整题干和学生答案另存两列，
    复核队列从那里读（docs/35 P0-4）。
    """
    stem80 = (problem_stem or "")[:80]
    reason = review_reason if review_reason in REVIEW_REASONS else "manual"
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO error_records(username,problem_id,knowledge_id,error_type,diagnosis,resolved,"
            "created_at,problem_stem,student_answer,review_reason,tech_detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (username, problem_id, knowledge_id, error_type,
             f"{stem80} || {diagnosis}" if stem80 else diagnosis, 0, _now(),
             (problem_stem or "")[:2000], (student_answer or "")[:4000], reason,
             (tech_detail or "")[:1000]),
        )
        return cur.lastrowid


def mark_error_review_requested(record_id: int, username: str) -> dict | None:
    """学生对自己的一条错题诊断说"请助教看看"。只能标自己的；已复核过的重新打开。"""
    with get_conn() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM error_records WHERE id=? AND username=?",
                          (record_id, username)).fetchone()
        if not row:
            return None
        cur.execute("UPDATE error_records SET review_reason='student_request', review_requested_at=?, "
                    "ta_reviewed=0 WHERE id=?", (_now(), record_id))
        return dict(cur.execute("SELECT * FROM error_records WHERE id=?", (record_id,)).fetchone())


def create_review_request(username: str, course_id: str, question: str, ai_answer: str,
                          source: str = "hall", note: str = "", no_evidence: bool = False) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO review_requests(username,course_id,source,question,ai_answer,note,no_evidence,"
            "status,created_at) VALUES(?,?,?,?,?,?,?,'open',?)",
            (username, course_id, source, question[:2000], ai_answer[:8000], note[:500],
             1 if no_evidence else 0, _now()))
        return cur.lastrowid


def list_review_requests(username: str = "", course_id: str = "", status: str = "",
                         limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    where, params = ["1=1"], []
    if username:
        where.append("username = ?")
        params.append(username)
    if course_id:
        where.append("course_id = ?")
        params.append(course_id)
    if status:
        where.append("status = ?")
        params.append(status)
    clause = " FROM review_requests WHERE " + " AND ".join(where)
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*)" + clause, params).fetchone()[0]
        rows = conn.execute("SELECT *" + clause + " ORDER BY status='answered', id DESC LIMIT ? OFFSET ?",
                            [*params, limit, max(0, offset)]).fetchall()
    return [dict(r) for r in rows], total


def answer_review_request(request_id: int, ta_username: str, verdict: str, reply: str) -> dict | None:
    with get_conn() as conn:
        cur = conn.cursor()
        if not cur.execute("SELECT 1 FROM review_requests WHERE id=?", (request_id,)).fetchone():
            return None
        cur.execute("UPDATE review_requests SET status='answered', ta_username=?, ta_verdict=?, "
                    "ta_reply=?, answered_at=? WHERE id=?",
                    (ta_username, verdict, reply[:2000], _now(), request_id))
        return dict(cur.execute("SELECT * FROM review_requests WHERE id=?", (request_id,)).fetchone())


def split_error_text(row: dict) -> tuple[str, str]:
    """(完整题干, 诊断)。新行读列，旧行从 diagnosis 的 "题干 || 诊断" 里切。"""
    raw = row.get("diagnosis") or ""
    stem, sep, diag = raw.partition(" || ")
    if not sep:
        stem, diag = "", raw
    return (row.get("problem_stem") or stem), diag


def save_attempt(username: str, answer: str, score: float, hints_used: int = 0,
                 duration: int = 0, problem_id: int = 0,
                 knowledge_id: int = 0, session_id: int = 0) -> int:
    """记一次作答。

    knowledge_id 必传或可推 —— 题库判题走 problem_id 就能反查，但 TutorGraph
    的自由问答没有题号，只能直接记知识点，否则这条作答在学情统计里等于不存在。
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO practice_attempts(username,problem_id,answer,score,hints_used,duration,"
            "created_at,knowledge_id,session_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (username, problem_id, answer, score, hints_used, duration, _now(),
             knowledge_id, session_id),
        )
        return cur.lastrowid


def upsert_mastery(username: str, knowledge_id: int, mastery: float, confidence: float, state: str) -> None:
    """写当前掌握度，并追加一条历史快照。

    两次写在同一个事务里：快照的意义是"当时的 mastery_records 长这样"，
    分开写就可能出现覆盖成功、快照丢失的错位。
    """
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mastery_records(username,knowledge_id,mastery,confidence,state,last_practiced_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(username,knowledge_id) DO UPDATE SET "
            "mastery=excluded.mastery, confidence=excluded.confidence, state=excluded.state, "
            "last_practiced_at=excluded.last_practiced_at",
            (username, knowledge_id, mastery, confidence, state, now),
        )
        conn.execute(
            "INSERT INTO mastery_snapshots(username,knowledge_id,mastery,state,created_at) "
            "VALUES(?,?,?,?,?)", (username, knowledge_id, mastery, state, now),
        )


# ---------- 知识点 / 会话留痕 ----------
def get_or_create_knowledge_id(name: str, course_id: str) -> int:
    """按 (course_id, 知识点名) 解析 id，无则建。空名返回 0。

    course_id 曾有默认值 "ds-algo"，四个调用点里三个吃了这个默认值 —— 于是
    co/os/net 的掌握度与错题被挂到新建的 ds-algo 重名空壳点上，而
    list_mastery / list_error_records 按 knowledge_points.course_id 内连接过滤，
    这些记录在科目视图下一条都看不到（脏数据修复见 scripts/fix_knowledge_course.py）。
    默认值在这里是陷阱不是便利，故改为必传。
    """
    name = (name or "").strip()
    if not name:
        return 0
    with get_conn() as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id FROM knowledge_points WHERE course_id=? AND name=?", (course_id, name)).fetchone()
        if row:
            return row["id"]
        cur.execute(
            "INSERT INTO knowledge_points(course_id,chapter,name,description,difficulty) VALUES(?,?,?,?,?)",
            (course_id, "", name, "", 2))
        return cur.lastrowid


def upsert_knowledge_point(course_id: str, chapter: str, name: str,
                           difficulty: int = 2, description: str = "") -> int:
    """播种/更新知识点（带章节归属+难度）。按 (course_id, name) 幂等：
    不存在则建，已存在则回填 chapter/difficulty/description（覆盖 get_or_create 建的空壳）。"""
    name = (name or "").strip()
    if not name:
        return 0
    with get_conn() as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id FROM knowledge_points WHERE course_id=? AND name=?", (course_id, name)).fetchone()
        if row:
            cur.execute(
                "UPDATE knowledge_points SET chapter=?, difficulty=?, "
                "description=CASE WHEN ?<>'' THEN ? ELSE description END WHERE id=?",
                (chapter, difficulty, description, description, row["id"]))
            return row["id"]
        cur.execute(
            "INSERT INTO knowledge_points(course_id,chapter,name,description,difficulty) VALUES(?,?,?,?,?)",
            (course_id, chapter, name, description, difficulty))
        return cur.lastrowid


def list_knowledge_points(course_id: str) -> list[dict]:
    """按章节顺序列出某门知识点（供知识树/章节浏览）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, chapter, name, description, difficulty FROM knowledge_points "
            "WHERE course_id=? ORDER BY id", (course_id,)).fetchall()
    return [dict(r) for r in rows]


def knowledge_id_of_problem(problem_id: int) -> tuple[int, str]:
    """题目 → (knowledge_id, course_id)。

    优先走 problem_knowledge 关联表 —— 那是种子脚本建的权威映射（376 行，已核实
    零课程错配），比 problems.knowledge 这个冗余字符串列可靠。没有关联时才按
    (course_id, name) 回落查同门同名知识点；再查不到返回 (0, course_id)，
    由调用方决定要不要 get_or_create。
    """
    if not problem_id:
        return 0, ""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT p.course_id, p.knowledge, l.knowledge_id FROM problems p "
            "LEFT JOIN problem_knowledge l ON l.problem_id = p.id WHERE p.id=?",
            (problem_id,)).fetchone()
        if not row:
            return 0, ""
        if row["knowledge_id"]:
            return row["knowledge_id"], row["course_id"]
        hit = conn.execute(
            "SELECT id FROM knowledge_points WHERE course_id=? AND name=?",
            (row["course_id"], (row["knowledge"] or "").strip())).fetchone()
    return (hit["id"] if hit else 0), row["course_id"]


def link_problem_knowledge(problem_id: int, knowledge_id: int, weight: float = 1.0) -> None:
    if not problem_id or not knowledge_id:
        return
    with get_conn() as conn:
        conn.execute("INSERT INTO problem_knowledge(problem_id,knowledge_id,weight) VALUES(?,?,?)",
                     (problem_id, knowledge_id, weight))


def create_session(username: str, course_id: str = "ds-algo", problem_id: int = 0,
                   intent: str = "chat", state: str = "active") -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO learning_sessions(username,course_id,problem_id,intent,state,started_at) "
            "VALUES(?,?,?,?,?,?)",
            (username, course_id, problem_id, intent, state, _now()))
        return cur.lastrowid


def save_turn(session_id: int, role: str, content: str, structured_data: str = "") -> None:
    """写一轮对话到 dialog_turns。session_id 为 0 时跳过（无会话上下文）。"""
    if not session_id:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO dialog_turns(session_id,role,content,structured_data,created_at) VALUES(?,?,?,?,?)",
            (session_id, role, content, structured_data, _now()))


def get_session(session_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM learning_sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def active_session(username: str, course_id: str, intent: str) -> dict | None:
    """该生该门最近一条尚未结束的会话。

    摸底页刷新、换设备、手滑关掉之后要能接着答 —— 能做到这一点，靠的是会话状态
    本来就在库里（见 list_turns），而不是像 TutorGraph 那样只活在进程内存中。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM learning_sessions WHERE username=? AND course_id=? AND intent=? "
            "AND completed_at IS NULL ORDER BY id DESC LIMIT 1",
            (username, course_id, intent)).fetchone()
    return dict(row) if row else None


def list_turns(session_id: int) -> list[dict]:
    """按时间顺序取会话的全部轮次。

    dialog_turns 建表以来一直只写不读。入学摸底把「问到第几题、当前难度多少、
    哪些章节已经覆盖」全部记成轮次，重放这些轮次就能还原会话状态 ——
    于是这张表第一次有了消费方，而摸底也不必再新开一张状态表。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, structured_data, created_at FROM dialog_turns "
            "WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def save_tutor_snapshot(db_session_id: int, session_id: str, username: str,
                        snapshot: str) -> None:
    """存 TutorGraph 会话快照（按 learning_sessions.id 唯一）。

    解题会话原先只活在 tutor_graph._SESSIONS 这个进程内字典里：后端一重启、
    或者会话被 MAX_SESSIONS 挤掉，学生手里的 session_id 就变成一句"会话不存在" ——
    题还在屏幕上，提交按钮却已经没用了。轮次一直在落库，缺的只是**状态机那几个数**
    （提示到第几级、试了几次、缓存的解析），这张表补的就是它们。
    """
    if not db_session_id:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tutor_snapshots(db_session_id,session_id,username,snapshot,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(db_session_id) DO UPDATE SET "
            "session_id=excluded.session_id, snapshot=excluded.snapshot, "
            "updated_at=excluded.updated_at",
            (db_session_id, session_id, username, snapshot, _now()))


def get_tutor_snapshot(session_id: str = "", db_session_id: int = 0) -> dict | None:
    """按 TutorGraph 会话 id 或 learning_sessions.id 取快照。"""
    if not session_id and not db_session_id:
        return None
    with get_conn() as conn:
        if session_id:
            row = conn.execute(
                "SELECT * FROM tutor_snapshots WHERE session_id=?", (session_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM tutor_snapshots WHERE db_session_id=?", (db_session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(username: str, course_id: str = "", intent: str = "",
                  limit: int = 20) -> list[dict]:
    """该生的会话列表，最近的在前，附首句摘要与轮次数。

    前端"回来接着看"靠的就是这张列表：进大厅时按 (course_id, intent) 取最近一条
    续上，点历史时把整列铺开。摘要取**第一条学生轮**而不是会话 id ——
    列表里认得出"哪一次是问栈和队列的"，才谈得上回去接着问。

    只有落过轮次的会话才会出现：/chat/stream 一进来就建会话行，若 LLM 当场失败、
    一句话没存下，那一行对用户不存在，列出来只会是一排点不开的空会话。
    """
    sql = ["SELECT s.id, s.course_id, s.intent, s.state, s.started_at, s.completed_at,",
           "       COUNT(t.id) AS turns, MAX(t.created_at) AS last_at,",
           "       (SELECT content FROM dialog_turns WHERE session_id=s.id AND role='student'",
           "         ORDER BY id LIMIT 1) AS title",
           "  FROM learning_sessions s JOIN dialog_turns t ON t.session_id = s.id",
           " WHERE s.username=?"]
    args: list = [username]
    if course_id:
        sql.append(" AND s.course_id=?")
        args.append(course_id)
    if intent:
        sql.append(" AND s.intent=?")
        args.append(intent)
    sql.append(" GROUP BY s.id ORDER BY s.id DESC LIMIT ?")
    args.append(max(1, min(int(limit or 20), 100)))
    with get_conn() as conn:
        rows = conn.execute("\n".join(sql), args).fetchall()
    return [dict(r) for r in rows]


def delete_session(session_id: int, username: str) -> bool:
    """删会话及其全部轮次；不是本人的会话一律返回 False（当作不存在）。

    轮次先删：反过来的话中途失败会留下一堆挂在空会话上的孤儿轮次，
    而 list_sessions 是 JOIN 出来的，孤儿轮次不会再有任何入口能看到它们。
    """
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM learning_sessions WHERE id=?",
                           (session_id,)).fetchone()
        if row is None or row["username"] != username:
            return False
        conn.execute("DELETE FROM dialog_turns WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM tutor_snapshots WHERE db_session_id=?", (session_id,))
        conn.execute("DELETE FROM learning_sessions WHERE id=?", (session_id,))
    return True


def last_completed_session(username: str, course_id: str, intent: str) -> dict | None:
    """该生该门最近一次做完的会话。摸底结果页刷新后重建报告用。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM learning_sessions WHERE username=? AND course_id=? AND intent=? "
            "AND state='completed' AND completed_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (username, course_id, intent)).fetchone()
    return dict(row) if row else None


def finish_session(session_id: int, state: str = "completed") -> bool:
    """结束会话；已经结束过的返回 False。

    `completed_at IS NULL` 是闸门：摸底收尾要写掌握度与错题，那一步只能发生一次。
    重复提交最后一题（或并发点两下）时，只有抢到这次 UPDATE 的调用方去落库。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE learning_sessions SET state=?, completed_at=? WHERE id=? AND completed_at IS NULL",
            (state, _now(), session_id))
        return cur.rowcount == 1


# ---------- 题库（problems / test_cases / rubric_items） ----------
def clear_problem_bank(course_id: str = "ds-algo", source: str = "") -> int:
    """清空某门题库。返回删除的题目数。

    ``source`` 为空时删该门**全部**题目（历史行为，seed_problems 重建题库时用）。
    传了 ``source`` 则只删该来源的题（如 ``seed-practice``）—— 重灌合成题时不会连带
    删掉校对过的真题（``past-paper``）和 grow_bank 定向补的题，那是原来最容易踩的坑。
    注意：带 source 过滤时**不重置** sqlite_sequence，否则新题 id 会与残留题目撞车。
    """
    with get_conn() as conn:
        c = conn.cursor()
        if source:
            rows = c.execute("SELECT id FROM problems WHERE course_id=? AND source=?",
                             (course_id, source)).fetchall()
        else:
            rows = c.execute("SELECT id FROM problems WHERE course_id=?", (course_id,)).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            qs = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM test_cases WHERE problem_id IN ({qs})", ids)
            c.execute(f"DELETE FROM rubric_items WHERE problem_id IN ({qs})", ids)
            c.execute(f"DELETE FROM problem_knowledge WHERE problem_id IN ({qs})", ids)
            c.execute(f"DELETE FROM problems WHERE id IN ({qs})", ids)
        if not source:
            # 重置自增序列，使重新灌库后 id 从 1 开始（演示更直观）
            for t in ("problems", "test_cases", "rubric_items"):
                c.execute("DELETE FROM sqlite_sequence WHERE name=?", (t,))
        return len(ids)


def insert_problem(course_id: str, ptype: str, stem: str, difficulty: int,
                   answer: str, verification_type: str, knowledge: str = "",
                   func_name: str = "", ref_code: str = "", source: str = "seed",
                   hint: str = "", citations: str = "") -> int:
    """写入一道题。`citations` 是出题所依据的教材片段（JSON 数组串），可为空。

    出处存成 JSON 串而不是外键表：它是**出题那一刻**的快照，不该随索引重建而变。
    资料撤下、语料重切之后，这道题依据的仍是当时那一段 —— 指向一张会变的表
    只会让旧题的出处慢慢变成别的东西，而且不会有任何报错。
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO problems(course_id,source,type,stem,difficulty,answer,verification_type,"
            "func_name,ref_code,knowledge,hint,citations) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (course_id, source, ptype, stem, difficulty, answer, verification_type,
             func_name, ref_code, knowledge, hint, citations))
        return cur.lastrowid


def add_test_case(problem_id: int, input_json: str, expected_json: str, hidden: int = 0) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO test_cases(problem_id,input,expected_output,hidden) VALUES(?,?,?,?)",
                     (problem_id, input_json, expected_json, hidden))


def add_rubric_item(problem_id: int, description: str, score: float, expected_evidence: str = "") -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO rubric_items(problem_id,description,score,expected_evidence) VALUES(?,?,?,?)",
                     (problem_id, description, score, expected_evidence))


def list_problems(course_id: str = "ds-algo", knowledge: str = "", limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        if knowledge:
            rows = conn.execute(
                "SELECT id,type,stem,difficulty,knowledge,verification_type FROM problems "
                "WHERE course_id=? AND knowledge=? ORDER BY id LIMIT ?", (course_id, knowledge, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,type,stem,difficulty,knowledge,verification_type FROM problems "
                "WHERE course_id=? ORDER BY id LIMIT ?", (course_id, limit)).fetchall()
    return [dict(r) for r in rows]


def pick_practice_problems(username: str, course_id: str, knowledge_id: int,
                           target_difficulty: int, limit: int = 3,
                           cooldown_days: int = 3) -> list[dict]:
    """为某个薄弱点选固定题：未做优先、答对排除、做错后冷却。

    题目归属只走 problem_knowledge，避免同名知识点跨课程串题。排序保持确定性，
    同一份学习记录会得到同一组推荐，便于解释和回归测试。
    """
    if not username or not knowledge_id or limit <= 0:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT p.id AS problem_id,p.stem,p.difficulty,p.verification_type,p.knowledge,p.type,p.source,"
            "COALESCE(a.tried,0) AS tried,a.best_score,a.last_at "
            "FROM problems p JOIN problem_knowledge pk ON pk.problem_id=p.id "
            "LEFT JOIN (SELECT problem_id,COUNT(*) AS tried,MAX(score) AS best_score,"
            "MAX(created_at) AS last_at FROM practice_attempts WHERE username=? GROUP BY problem_id) a "
            "ON a.problem_id=p.id "
            "WHERE p.course_id=? AND pk.knowledge_id=? AND COALESCE(a.best_score,0)<60 "
            "AND (a.last_at IS NULL OR julianday('now','localtime')-julianday(a.last_at)>=?) "
            "ORDER BY CASE WHEN a.tried IS NULL THEN 0 ELSE 1 END,"
            "ABS(COALESCE(p.difficulty,2)-?),p.id LIMIT ?",
            (username, course_id, knowledge_id, cooldown_days,
             max(1, min(5, target_difficulty)), limit),
        ).fetchall()
    return [dict(r) for r in rows]


_BANK_POOL_SQL = """
SELECT p.id AS problem_id, p.stem, p.type, p.answer, p.verification_type,
       COALESCE(p.difficulty, 2) AS difficulty, p.source,
       k.id AS knowledge_id, k.name AS knowledge, COALESCE(k.chapter, '') AS chapter,
       CASE WHEN a.problem_id IS NULL THEN 0 ELSE 1 END AS seen
FROM problems p
JOIN (SELECT problem_id, MIN(knowledge_id) AS knowledge_id
      FROM problem_knowledge GROUP BY problem_id) pk ON pk.problem_id = p.id
JOIN knowledge_points k ON k.id = pk.knowledge_id AND k.course_id = p.course_id
LEFT JOIN (SELECT DISTINCT problem_id FROM practice_attempts WHERE username = ?) a
       ON a.problem_id = p.id
WHERE p.course_id = ?
ORDER BY p.id
"""


def bank_pool(username: str, course_id: str) -> list[dict]:
    """该门**能归到知识点**的全部题目，附带"这个人做过没有"。摸底与模考组卷的共同取数口。

    必须有 problem_knowledge 关联才收：这两处的产出都要写回掌握度和错题本，
    落不到知识点上的题做了也写不进去（同 grade_photo_collect 的理由）。
    关联表取 MIN 保证一题一点，`k.course_id = p.course_id` 挡住同名知识点串课。
    `seen` 供选题时优先出没做过的 —— 做过的题测的是记忆，不是水平。
    """
    with get_conn() as conn:
        rows = conn.execute(_BANK_POOL_SQL, (username, course_id)).fetchall()
    return [dict(r) for r in rows]


def placement_pool(username: str, course_id: str) -> list[dict]:
    """入学摸底的可选题池：`bank_pool` 里**可确定性判定**的那部分。

    只收 `verification_type='numeric'` 且 answer 非空的题：摸底要一题接一题当场判、
    当场决定下一题出多难，不能每道题等一次模型。canonical 校验（check_numeric）
    是唯一既快又可复现的判法；代码题要跑沙箱、概念题的得分点覆盖判不出来时还得
    回落 LLM，都不适合摸底。模考没有这个限制（它一次性交卷，慢一点可以接受）。
    """
    return [p for p in bank_pool(username, course_id)
            if p["verification_type"] == "numeric" and (p["answer"] or "").strip()]


def problem_id_by_stem(course_id: str, stem: str) -> int:
    """同门题干精确去重（忽略首尾空白）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM problems WHERE course_id=? AND trim(stem)=? ORDER BY id LIMIT 1",
            (course_id, (stem or "").strip()),
        ).fetchone()
    return int(row["id"]) if row else 0


def get_problem(problem_id: int, with_solution: bool = False) -> dict | None:
    import json as _json
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM problems WHERE id=?", (problem_id,)).fetchone()
        if not row:
            return None
        p = dict(row)
        tcs = conn.execute("SELECT input,expected_output,hidden FROM test_cases WHERE problem_id=?",
                           (problem_id,)).fetchall()
        rubric = conn.execute("SELECT description,score,expected_evidence FROM rubric_items WHERE problem_id=?",
                              (problem_id,)).fetchall()
    cases = [{"args": _json.loads(t["input"]), "expected": _json.loads(t["expected_output"]),
              "hidden": bool(t["hidden"])} for t in tcs]
    out = {"id": p["id"], "course_id": p["course_id"], "type": p["type"], "stem": p["stem"],
           "difficulty": p["difficulty"], "knowledge": p["knowledge"],
           "verification_type": p["verification_type"], "source": p["source"],
           "func_name": p["func_name"], "test_count": len(cases), "rubric": [dict(r) for r in rubric],
           # has_hint 而不是 hint 本身：提示要"点了才给"，先发过去等于白送。
           # 真正的文本走 get_problem_hint()，那一步才计入 hints_used。
           "has_hint": bool((p["hint"] or "").strip()),
           # 出题所依据的教材出处。与 hint 不同，这个不藏 —— 它不泄露答案，
           # 而"这题出自教材哪一页"正是学生做题时最该看到的东西。
           "citations": _json.loads(p["citations"]) if (p.get("citations") or "").strip() else []}
    # numeric 题：暴露 canonical 的 kind/unit/是否多选（供前端渲染结构化控件，不泄露答案值）
    if p["verification_type"] == "numeric" and p.get("answer"):
        try:
            canon = _json.loads(p["answer"])
            out["answer_kind"] = canon.get("kind")
            if canon.get("unit"):
                out["answer_unit"] = canon["unit"]
            if canon.get("kind") == "choice":
                out["answer_multi"] = isinstance(canon.get("value"), list)
        except (_json.JSONDecodeError, TypeError, AttributeError):
            pass
    if with_solution:
        out.update({"answer": p["answer"], "func_name": p["func_name"], "ref_code": p["ref_code"],
                    "test_cases": cases})
    return out


def get_problem_hint(problem_id: int) -> str:
    """取题目的一级提示（不泄露答案）。没有提示返回空串。"""
    with get_conn() as conn:
        row = conn.execute("SELECT hint FROM problems WHERE id=?", (problem_id,)).fetchone()
    return (row["hint"] or "").strip() if row else ""


def knowledge_distribution(course_id: str = "ds-algo") -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT knowledge, COUNT(*) AS cnt FROM problems WHERE course_id=? GROUP BY knowledge ORDER BY cnt DESC",
            (course_id,)).fetchall()
    return [dict(r) for r in rows]


def list_error_records(username: str, limit: int = 50, course: str = "") -> list[dict]:
    with get_conn() as conn:
        if course:
            # 按科目过滤：错题的知识点须归属该门（knowledge_id=0 的旧错题在科目视图下不展示）
            rows = conn.execute(
                "SELECT e.* FROM error_records e JOIN knowledge_points k ON k.id=e.knowledge_id "
                "WHERE e.username=? AND k.course_id=? ORDER BY e.id DESC LIMIT ?",
                (username, course, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM error_records WHERE username=? ORDER BY id DESC LIMIT ?",
                (username, limit)).fetchall()
    return [dict(r) for r in rows]


def get_mastery(username: str, knowledge_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT mastery, confidence, state, last_practiced_at, review_stage, next_due_at "
            "FROM mastery_records WHERE username=? AND knowledge_id=?",
            (username, knowledge_id)).fetchone()
    return dict(row) if row else None


def list_mastery(username: str, course: str = "") -> list[dict]:
    with get_conn() as conn:
        if course:
            rows = conn.execute(
                "SELECT m.*, COALESCE(k.name, '知识点' || m.knowledge_id) AS knowledge_name "
                "FROM mastery_records m JOIN knowledge_points k ON k.id = m.knowledge_id "
                "WHERE m.username=? AND k.course_id=? ORDER BY m.mastery ASC", (username, course)).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.*, COALESCE(k.name, '知识点' || m.knowledge_id) AS knowledge_name "
                "FROM mastery_records m LEFT JOIN knowledge_points k ON k.id = m.knowledge_id "
                "WHERE m.username=? ORDER BY m.mastery ASC", (username,)).fetchall()
    return [dict(r) for r in rows]


# ---------- 学情聚合（个性化练习与复习计划共用的取数层） ----------
# 这一段只出原始聚合，不做任何衰减/评分 —— 那是 backend/learning/profile.py 的事。
# 纪律：一切以 knowledge_points.course_id 为锚，题→知识点走 problem_knowledge
# 关联表，同名字符串只作兜底。串课 bug 的教训就是"按名字对齐"靠不住。

# kp被题目映射和最终画像多次复用；显式物化避免不同SQLite优化器
# 对部分引用展开后重复计算课程范围，同时保留原来的全部关联语义。
_KNOWLEDGE_STATS_SQL = """
WITH kp AS MATERIALIZED (
  SELECT id, name, chapter, difficulty FROM knowledge_points WHERE course_id = :course),
pk AS MATERIALIZED (
  SELECT p.id AS problem_id, COALESCE(l.knowledge_id, k2.id) AS knowledge_id
  FROM problems p
  LEFT JOIN problem_knowledge l ON l.problem_id = p.id
  LEFT JOIN kp k2 ON k2.name = p.knowledge
  WHERE p.course_id = :course),
-- 题库存量先按知识点汇总一次。原先写成 SELECT 里的逐行子查询
-- `(SELECT COUNT(*) FROM pk WHERE pk.knowledge_id = kp.id)`，每个知识点都把 pk
-- （整门课上千道题的映射）重算一遍，一次画像约 0.39s，学习中心一次请求要算两次。
bank AS (
  SELECT knowledge_id, COUNT(*) AS n FROM pk GROUP BY knowledge_id),
-- 一次作答归到哪个知识点：优先按题目反查（题库判题），题目查不到就用作答自带的
-- knowledge_id（TutorGraph 的自由问答没有题号）。原先只认前者，且带
-- `a.problem_id > 0` 过滤，于是智能解题页产生的作答在学情里整个不存在。
resolved_att AS (
  SELECT a.id, a.score, a.created_at,
         COALESCE(NULLIF(pk.knowledge_id, 0), NULLIF(a.knowledge_id, 0)) AS kid
  FROM practice_attempts a
  LEFT JOIN pk ON pk.problem_id = a.problem_id
  WHERE a.username = :u),
att AS (
  SELECT kid, COUNT(*) AS attempts,
         SUM(CASE WHEN score >= 60 THEN 1 ELSE 0 END) AS correct_n,
         MAX(id) AS last_id, MAX(created_at) AS last_at
  FROM resolved_att WHERE kid IS NOT NULL
  GROUP BY kid),
err AS (
  SELECT e.knowledge_id AS kid, COUNT(*) AS error_total,
         SUM(CASE WHEN e.resolved = 0 THEN 1 ELSE 0 END) AS error_open,
         MAX(e.created_at) AS last_err_at
  FROM error_records e JOIN kp ON kp.id = e.knowledge_id
  WHERE e.username = :u
  GROUP BY e.knowledge_id)
SELECT kp.id AS knowledge_id, kp.name, kp.chapter, kp.difficulty AS kp_difficulty,
       m.mastery, m.confidence, m.state, m.last_practiced_at, m.review_stage, m.next_due_at,
       COALESCE(att.attempts, 0) AS attempts, COALESCE(att.correct_n, 0) AS correct_n,
       att.last_at, la.score AS last_score,
       COALESCE(err.error_total, 0) AS error_total, COALESCE(err.error_open, 0) AS error_open,
       err.last_err_at,
       COALESCE(bank.n, 0) AS bank_count
FROM kp
LEFT JOIN mastery_records m ON m.username = :u AND m.knowledge_id = kp.id
LEFT JOIN att ON att.kid = kp.id
LEFT JOIN practice_attempts la ON la.id = att.last_id
LEFT JOIN err ON err.kid = kp.id
LEFT JOIN bank ON bank.knowledge_id = kp.id
"""


def knowledge_stats(username: str, course: str) -> list[dict]:
    """某生某门每个知识点的原始聚合：掌握度 + 作答 + 错题 + 题库存量。

    覆盖该门**全部**知识点（含从未练过的），由调用方决定要不要筛 —— 冷启动时
    "还没碰过的章节"同样是要推荐的内容。
    """
    with get_conn() as conn:
        rows = conn.execute(_KNOWLEDGE_STATS_SQL, {"u": username, "course": course}).fetchall()
    return [dict(r) for r in rows]


def error_types_by_knowledge(username: str, course: str,
                             open_only: bool = True) -> dict[int, list[tuple[str, int]]]:
    """{knowledge_id: [(错因 code, 次数), ...] 按次数降序}。供出题时注入学生错因。"""
    sql = ("SELECT e.knowledge_id AS kid, e.error_type, COUNT(*) AS n FROM error_records e "
           "JOIN knowledge_points k ON k.id = e.knowledge_id "
           "WHERE e.username=? AND k.course_id=?"
           + (" AND e.resolved=0" if open_only else "")
           + " GROUP BY 1, 2 ORDER BY 1, n DESC")
    out: dict[int, list[tuple[str, int]]] = {}
    with get_conn() as conn:
        for r in conn.execute(sql, (username, course)):
            out.setdefault(r["kid"], []).append((r["error_type"], r["n"]))
    return out


def set_mastery_state(username: str, knowledge_id: int, state: str) -> None:
    """只改状态，不动掌握度数值与"上次练习时间"。

    给助教改判用：改判改的是"这次错在哪"，不是"学生又练了一次" —— 走 upsert_mastery
    会把 last_practiced_at 刷成现在，遗忘衰减和复习到期全被打乱。
    """
    with get_conn() as conn:
        conn.execute("UPDATE mastery_records SET state=? WHERE username=? AND knowledge_id=?",
                     (state, username, knowledge_id))


def has_open_misconception(username: str, knowledge_id: int, tags: tuple[str, ...]) -> bool:
    """这个知识点上还有没有未解决、且判为概念类错因的错题。"""
    holes = ",".join("?" * len(tags))
    with get_conn() as conn:
        return conn.execute(
            f"SELECT 1 FROM error_records WHERE username=? AND knowledge_id=? AND COALESCE(resolved,0)=0 "
            f"AND error_type IN ({holes}) LIMIT 1", (username, knowledge_id, *tags)).fetchone() is not None


def had_correct_attempt(username: str, knowledge_id: int) -> bool:
    """该生在这个知识点上**有没有做对过**（careless 门槛的举证依据）。"""
    if not knowledge_id:
        return False
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM practice_attempts WHERE username=? AND knowledge_id=? "
                            "AND score >= 60 LIMIT 1", (username, knowledge_id)).fetchone() is not None


def last_attempt_correct(username: str, knowledge_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT score FROM practice_attempts WHERE username=? AND knowledge_id=? "
                           "ORDER BY id DESC LIMIT 1", (username, knowledge_id)).fetchone()
    return bool(row) and (row["score"] or 0) >= 60


def count_attempts(username: str, knowledge_id: int) -> int:
    """该生在某知识点上的真实作答次数。

    三条来源取并集：① 作答直接记了知识点（TutorGraph 路径）；
    ② 走 problem_knowledge 关联表；③ 同门同名知识点兜底。
    只认第二、三条的话，智能解题页的作答会被漏掉（它们没有题号）。
    """
    if not knowledge_id:
        return 0
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM practice_attempts a WHERE a.username=? AND ("
            "  a.knowledge_id=?"
            "  OR (a.problem_id > 0 AND a.problem_id IN ("
            "    SELECT l.problem_id FROM problem_knowledge l WHERE l.knowledge_id=?"
            "    UNION"
            "    SELECT p.id FROM problems p JOIN knowledge_points k ON k.id=? "
            "     WHERE p.course_id=k.course_id AND p.knowledge=k.name)))",
            (username, knowledge_id, knowledge_id, knowledge_id)).fetchone()[0]


def count_open_errors(username: str, knowledge_id: int, error_type: str = "") -> int:
    """该生在某知识点上未解决的错题数（可按错因过滤）。判"是否重复犯同一类错"用。"""
    if not knowledge_id:
        return 0
    sql = "SELECT COUNT(*) FROM error_records WHERE username=? AND knowledge_id=? AND resolved=0"
    params: list = [username, knowledge_id]
    if error_type:
        sql += " AND error_type=?"
        params.append(error_type)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()[0]


def resolve_error_records(username: str, knowledge_id: int, problem_id: int = 0,
                          all_of_point: bool = False) -> int:
    """答对后销掉错题，返回销掉的条数。

    error_records.resolved 一直恒 0（无人更新），"错题复习率"这个评审指标因此
    没有数据来源。策略是确定性的、可解释的：同一道题的未解决错题一律销掉；
    若该知识点的掌握度已跨过阈值，该点全部未解决错题一并销掉。
    """
    if not knowledge_id and not problem_id:
        return 0
    with get_conn() as conn:
        if all_of_point and knowledge_id:
            cur = conn.execute(
                "UPDATE error_records SET resolved=1, resolved_at=? "
                "WHERE username=? AND knowledge_id=? AND resolved=0", (_now(), username, knowledge_id))
        elif problem_id:
            cur = conn.execute(
                "UPDATE error_records SET resolved=1, resolved_at=? "
                "WHERE username=? AND problem_id=? AND resolved=0", (_now(), username, problem_id))
        else:
            return 0
        return cur.rowcount


def set_review_stage(username: str, knowledge_id: int, stage: int, next_due_at: str = "") -> None:
    """推进间隔复习的 stage。只碰这三列，不动判题写回的 mastery/state。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mastery_records(username,knowledge_id,mastery,confidence,state,"
            "review_stage,next_due_at,last_reviewed_at) VALUES(?,?,0,0,'learning',?,?,?) "
            "ON CONFLICT(username,knowledge_id) DO UPDATE SET "
            "review_stage=excluded.review_stage, next_due_at=excluded.next_due_at, "
            "last_reviewed_at=excluded.last_reviewed_at",
            (username, knowledge_id, stage, next_due_at, _now()))


# ---------- 备考时间预算 ----------

def get_study_budget(username: str) -> dict | None:
    """取该生的时间预算；没填过返回 None（由 study_budget 层决定默认值）。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM study_budgets WHERE username=?",
                           (username,)).fetchone()
        if not row:
            return None
        budget = dict(row)
        budget["exceptions"] = [
            dict(r) for r in conn.execute(
                "SELECT date,minutes,note FROM study_budget_exceptions "
                "WHERE username=? ORDER BY date", (username,)).fetchall()
        ]
    return budget


def save_study_budget(username: str, weekly_minutes: str, intensity: str,
                      buffer_weekday: int, exam_date: str,
                      exceptions: list[dict]) -> dict:
    """整体覆盖写。例外日先删后插 —— 界面上编辑的就是"这一整张表"，
    增量合并会让"删掉一个例外日"这个操作没有对应的写入路径。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO study_budgets(username,weekly_minutes,intensity,buffer_weekday,"
            "exam_date,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET weekly_minutes=excluded.weekly_minutes,"
            "intensity=excluded.intensity, buffer_weekday=excluded.buffer_weekday,"
            "exam_date=excluded.exam_date, updated_at=excluded.updated_at",
            (username, weekly_minutes, intensity, buffer_weekday, exam_date, _now()),
        )
        conn.execute("DELETE FROM study_budget_exceptions WHERE username=?", (username,))
        for e in exceptions:
            conn.execute(
                "INSERT OR REPLACE INTO study_budget_exceptions(username,date,minutes,note) "
                "VALUES(?,?,?,?)",
                (username, e["date"], int(e["minutes"]), (e.get("note") or "")[:60]),
            )
    return get_study_budget(username) or {}


def attempt_duration_median(username: str, limit: int = 60) -> int:
    """最近 limit 次作答用时的中位数（秒）。没有可用样本返回 0。

    duration 有大量 0（老数据、未计时的路径），它们不是"零秒答完"而是"没记"，
    取中位数前必须滤掉，否则一半样本是 0 会把中位数直接压到 0。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT duration FROM practice_attempts WHERE username=? AND duration>0 "
            "ORDER BY id DESC LIMIT ?", (username, limit)).fetchall()
    vals = sorted(int(r["duration"]) for r in rows)
    if not vals:
        return 0
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) // 2


# ---------- 复习计划持久化 ----------

def create_review_plan(username: str, course_id: str, goal: str, start_date: str,
                       exam_date: str, daily_minutes: int, summary: str,
                       tasks: list[dict], baseline_mastery: float,
                       baseline_open_errors: int, *, budget_snapshot: str = "",
                       materialized_through: str = "", milestones: str = "") -> dict:
    """原子创建活动计划；同用户同课程的旧活动计划只标记失效，不删除。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE review_plans SET status='superseded' "
            "WHERE username=? AND course_id=? AND status='active'",
            (username, course_id),
        )
        span = max((int(t.get("day", 0)) for t in tasks), default=0)
        cur = conn.execute(
            "INSERT INTO review_plans(username,course_id,goal,start_date,exam_date,status,"
            "daily_minutes,created_at,summary,span_days,baseline_mastery,baseline_open_errors,"
            "budget_snapshot,materialized_through,milestones) "
            "VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?)",
            (username, course_id, goal, start_date, exam_date, daily_minutes, _now(),
             summary, span, baseline_mastery, baseline_open_errors,
             budget_snapshot, materialized_through or exam_date, milestones),
        )
        plan_id = int(cur.lastrowid)
        _insert_review_tasks(conn, plan_id, tasks)
    return get_active_review_plan(username, course_id) or {"id": plan_id, "tasks": []}


def _insert_review_tasks(conn: sqlite3.Connection, plan_id: int, tasks: list[dict]) -> None:
    """建计划与滚动续排共用的写入。due_from/due_to 缺省时回落到 task_date ——
    不带窗口的调用方（旧 build_plan 路径）因此得到"窗口宽度为 0"，逾期判定与从前一致。"""
    for t in tasks:
        task_date = t.get("task_date", "")
        conn.execute(
            "INSERT INTO review_tasks(plan_id,knowledge_id,task_date,task_type,target_count,completed,"
            "day_index,review_round,stage,interval_days,minutes,reason,priority,error_type,"
            "completed_count,created_at,tier,due_from,due_to,dropped) "
            "VALUES(?,?,?,?,?,0,?,?,?,?,?,?,?,?,0,?,?,?,?,0)",
            (plan_id, t.get("knowledge_id", 0), task_date,
             t.get("task_type", "针对性练习"), t.get("target_count", 1),
             t.get("day", 0), t.get("round", 1), t.get("stage", 0),
             t.get("interval", 0), t.get("minutes", 15), t.get("reason", ""),
             t.get("priority", 0), t.get("error_type", ""), _now(),
             t.get("tier", "core"), t.get("due_from") or task_date,
             t.get("due_to") or task_date),
        )


def append_review_tasks(plan_id: int, tasks: list[dict]) -> None:
    """滚动窗口续排：往已有计划里追加任务行，不动已有的任何一行。"""
    if not tasks:
        return
    with get_conn() as conn:
        _insert_review_tasks(conn, plan_id, tasks)


def update_review_plan_scheduling(plan_id: int, *, materialized_through: str = "",
                                  reconciled_on: str = "", milestones: str | None = None) -> None:
    """只碰排期元数据三列，不动 baseline_* 那几个快照值。"""
    sets, params = [], []
    if materialized_through:
        sets.append("materialized_through=?")
        params.append(materialized_through)
    if reconciled_on:
        sets.append("reconciled_on=?")
        params.append(reconciled_on)
    if milestones is not None:
        sets.append("milestones=?")
        params.append(milestones)
    if not sets:
        return
    params.append(plan_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE review_plans SET {','.join(sets)} WHERE id=?", params)


def reschedule_review_task(task_id: int, *, day_index: int, task_date: str,
                           due_from: str, due_to: str, tier: str = "",
                           dropped: bool | None = None) -> None:
    """自愈重排用：挪窗口、可选地降级或放弃。stage/interval 不动 ——
    那两个值属于间隔复习的进度，只应由"做完了"来推进，不该被"没做完"改写。"""
    sets = ["day_index=?", "task_date=?", "due_from=?", "due_to=?"]
    params: list = [day_index, task_date, due_from, due_to]
    if tier:
        sets.append("tier=?")
        params.append(tier)
    if dropped is not None:
        sets.append("dropped=?")
        params.append(1 if dropped else 0)
    params.append(task_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE review_tasks SET {','.join(sets)} WHERE id=?", params)


def _review_plan_payload(conn: sqlite3.Connection, plan_row) -> dict:
    plan = dict(plan_row)
    rows = conn.execute(
        "SELECT t.*,COALESCE(k.name,'知识点'||t.knowledge_id) AS name "
        "FROM review_tasks t LEFT JOIN knowledge_points k ON k.id=t.knowledge_id "
        "WHERE t.plan_id=? ORDER BY t.day_index,t.id", (plan["id"],),
    ).fetchall()
    plan["tasks"] = [dict(r) for r in rows]
    return plan


def get_active_review_plan(username: str, course_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM review_plans WHERE username=? AND course_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 1", (username, course_id),
        ).fetchone()
        return _review_plan_payload(conn, row) if row else None


def get_review_task(task_id: int, username: str = "") -> dict | None:
    with get_conn() as conn:
        sql = ("SELECT t.*,p.username,p.course_id,p.start_date,p.exam_date,p.status,p.daily_minutes "
               "FROM review_tasks t JOIN review_plans p ON p.id=t.plan_id WHERE t.id=?")
        params: list = [task_id]
        if username:
            sql += " AND p.username=?"
            params.append(username)
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def list_review_tasks(plan_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM review_tasks WHERE plan_id=? ORDER BY day_index,id", (plan_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def complete_review_task(task_id: int, completed_count: int | None = None) -> None:
    with get_conn() as conn:
        if completed_count is None:
            conn.execute(
                "UPDATE review_tasks SET completed=1,completed_count=target_count,completed_at=? "
                "WHERE id=? AND completed=0", (_now(), task_id),
            )
        else:
            conn.execute(
                "UPDATE review_tasks SET completed_count=MAX(completed_count,?),"
                "completed=CASE WHEN ?>=target_count THEN 1 ELSE completed END,"
                "completed_at=CASE WHEN ?>=target_count AND completed_at IS NULL THEN ? ELSE completed_at END "
                "WHERE id=?",
                (completed_count, completed_count, completed_count, _now(), task_id),
            )


def increment_review_task(task_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE review_tasks SET completed_count=MIN(target_count,completed_count+1),"
            "completed=CASE WHEN completed_count+1>=target_count THEN 1 ELSE completed END,"
            "completed_at=CASE WHEN completed_count+1>=target_count AND completed_at IS NULL THEN ? "
            "ELSE completed_at END WHERE id=? AND completed=0", (_now(), task_id),
        )
        row = conn.execute("SELECT * FROM review_tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def claim_review_task_problem(task_id: int, problem_id: int,
                              correct: bool, hints_used: int = 0) -> bool:
    """同一复习任务中同一道题只计一次，防止重复点提交刷满目标题数。"""
    if not task_id or not problem_id:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO review_task_attempts(task_id,problem_id,correct,hints_used,created_at) "
            "VALUES(?,?,?,?,?)",
            (task_id, problem_id, 1 if correct else 0, max(0, hints_used), _now()),
        )
        return cur.rowcount == 1


def review_task_performance(task_id: int) -> tuple[bool, int]:
    """一轮中任一题答错就按答错调度；提示数取本轮最大值。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MIN(correct) AS all_correct,MAX(hints_used) AS hints "
            "FROM review_task_attempts WHERE task_id=?", (task_id,),
        ).fetchone()
    return bool(row["all_correct"]), int(row["hints"] or 0)


def update_review_task_schedule(task_id: int, day_index: int, task_date: str,
                                stage: int, interval_days: int,
                                due_from: str = "", due_to: str = "") -> None:
    """完成一轮后推进后续轮次。到期窗口必须跟着一起挪 —— 只改 task_date 的话，
    后面几轮会带着旧窗口，一落地就被判成逾期，然后被自愈重排立刻又搬一次。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE review_tasks SET day_index=?,task_date=?,stage=?,interval_days=?,"
            "due_from=?,due_to=? WHERE id=?",
            (day_index, task_date, stage, interval_days,
             due_from or task_date, due_to or task_date, task_id),
        )


def review_progress(username: str, course_id: str) -> dict | None:
    plan = get_active_review_plan(username, course_id)
    if not plan:
        return None
    # 被放弃的轮次不进分母：它们是系统主动减负的结果，让完成率因为"帮你减了负"
    # 反而下降，等于惩罚了这条机制本身。
    tasks = [t for t in plan["tasks"] if not t.get("dropped")]
    total = len(tasks)
    done = sum(bool(t["completed"]) for t in tasks)
    kids = sorted({int(t["knowledge_id"]) for t in tasks if t["knowledge_id"]})
    current_mastery = 0.0
    open_errors = 0
    if kids:
        qs = ",".join("?" for _ in kids)
        with get_conn() as conn:
            vals = conn.execute(
                f"SELECT mastery FROM mastery_records WHERE username=? AND knowledge_id IN ({qs})",
                (username, *kids),
            ).fetchall()
            current_mastery = sum(float(r["mastery"] or 0) for r in vals) / len(kids)
            open_errors = conn.execute(
                f"SELECT COUNT(*) FROM error_records WHERE username=? AND resolved=0 "
                f"AND knowledge_id IN ({qs})", (username, *kids),
            ).fetchone()[0]
    baseline_errors = int(plan.get("baseline_open_errors") or 0)
    error_rate = ((baseline_errors - open_errors) / baseline_errors) if baseline_errors else 0.0
    return {"plan_id": plan["id"], "completion_rate": round(done / total, 4) if total else 0.0,
            "completed_tasks": done, "total_tasks": total,
            "open_errors": open_errors,
            "error_resolve_rate": round(max(0.0, error_rate), 4),
            "mastery_delta": round(current_mastery - float(plan.get("baseline_mastery") or 0), 4)}


def list_attempts(username: str, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM practice_attempts WHERE username=? ORDER BY id DESC LIMIT ?",
            (username, limit)).fetchall()
    return [dict(r) for r in rows]


# 某生某门的全部作答：题库判题按题目归门，TutorGraph 作答按知识点归门。
# 原先只有前半段（JOIN problems），于是学习中心的「作答次数 / 正确率 / 连续天数」
# 全都看不见智能解题页的作答 —— 而那恰恰是最能体现产品特色的一页。
# 作答属于哪门课：题目属于这门课，或作答记在这门课的知识点上。学生端和助教端
# 的作答次数都用这一个判据（I-03）—— 此前助教端只认 knowledge_id，两边可能数出不同的数。
_ATTEMPT_IN_COURSE = ("(a.problem_id IN (SELECT id FROM problems WHERE course_id = ?) "
                      "OR a.knowledge_id IN (SELECT id FROM knowledge_points WHERE course_id = ?))")

_COURSE_ATTEMPTS_SQL = """
SELECT a.* FROM practice_attempts a
WHERE a.username = :u AND (
  a.problem_id IN (SELECT id FROM problems WHERE course_id = :course)
  OR a.knowledge_id IN (SELECT id FROM knowledge_points WHERE course_id = :course))
"""


def error_counts(username: str, course_id: str) -> dict:
    """某生某门的错题计数（累计）。学习首页与错题记录页共用，两处的数必须一样。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN e.resolved=0 THEN 1 ELSE 0 END) AS open,"
            "SUM(CASE WHEN e.resolved=1 THEN 1 ELSE 0 END) AS resolved "
            "FROM error_records e JOIN knowledge_points k ON k.id=e.knowledge_id "
            "WHERE e.username=? AND k.course_id=?", (username, course_id),
        ).fetchone()
    return {"total": int(row["total"] or 0), "open": int(row["open"] or 0),
            "resolved": int(row["resolved"] or 0)}


def learning_activity_summary(username: str, course_id: str, limit: int = 8) -> dict:
    """个人学习中心的轻量活动聚合；只统计当前课程，不跨科目混合。"""
    scoped = f"({_COURSE_ATTEMPTS_SQL})"
    params = {"u": username, "course": course_id}
    with get_conn() as conn:
        stats = conn.execute(
            "SELECT COUNT(*) AS attempts,"
            "SUM(CASE WHEN a.score>=60 THEN 1 ELSE 0 END) AS correct,"
            "SUM(CASE WHEN date(a.created_at)=date('now','localtime') THEN 1 ELSE 0 END) AS today,"
            "SUM(COALESCE(a.duration,0)) AS duration "
            f"FROM {scoped} a", params,
        ).fetchone()
        # 最近作答：题库题取题干，自由问答没有题目行，用知识点名占位（LEFT JOIN 而非 JOIN）
        recent = conn.execute(
            "SELECT a.id,a.problem_id,a.score,a.duration,a.hints_used,a.created_at,"
            "COALESCE(p.stem, '自由作答：' || COALESCE(k.name,'未归类')) AS stem,"
            "COALESCE(p.knowledge, k.name, '') AS knowledge,"
            "COALESCE((SELECT pk.knowledge_id FROM problem_knowledge pk "
            "WHERE pk.problem_id=p.id ORDER BY pk.weight DESC LIMIT 1), a.knowledge_id, 0) AS knowledge_id "
            f"FROM {scoped} a "
            "LEFT JOIN problems p ON p.id=a.problem_id "
            "LEFT JOIN knowledge_points k ON k.id=a.knowledge_id "
            "ORDER BY a.id DESC LIMIT :lim",
            {**params, "lim": max(1, min(30, limit))},
        ).fetchall()
        days = conn.execute(
            f"SELECT DISTINCT date(a.created_at) AS day FROM {scoped} a "
            "ORDER BY day DESC LIMIT 366", params,
        ).fetchall()
    errs = error_counts(username, course_id)
    return {
        "attempts": int(stats["attempts"] or 0),
        "correct": int(stats["correct"] or 0),
        "today_attempts": int(stats["today"] or 0),
        "duration_seconds": int(stats["duration"] or 0),
        "errors_total": errs["total"],
        "open_errors": errs["open"],
        "resolved_errors": errs["resolved"],
        "activity_days": [r["day"] for r in days if r["day"]],
        "recent_attempts": [dict(r) for r in recent],
    }


# ---------- 学情周报：按时间窗口的聚合 ----------
# 与上面的 learning_activity_summary 是两个口径，别合并：那个出"截至此刻的累计"，
# 这里出"[start, end) 这一段里发生了什么"。周报要的是后者 —— 拿累计量讲"这周"，
# 是这类报表最常见也最难被发现的假。

def window_activity(username: str, course_id: str, start: str, end: str) -> dict:
    """窗口内的作答聚合。start/end 为 'YYYY-MM-DD'，左闭右开（end 当天不计入）。"""
    scoped = f"({_COURSE_ATTEMPTS_SQL})"
    params = {"u": username, "course": course_id, "start": start, "end": end}
    win = "date(a.created_at) >= date(:start) AND date(a.created_at) < date(:end)"
    with get_conn() as conn:
        stats = conn.execute(
            "SELECT COUNT(*) AS attempts,"
            "SUM(CASE WHEN a.score>=60 THEN 1 ELSE 0 END) AS correct,"
            "SUM(CASE WHEN COALESCE(a.hints_used,0)>0 THEN 1 ELSE 0 END) AS hinted,"
            "SUM(COALESCE(a.duration,0)) AS duration,"
            "COUNT(DISTINCT date(a.created_at)) AS active_days "
            f"FROM {scoped} a WHERE {win}", params,
        ).fetchone()
        # 窗口内练过的知识点。归属口径与 _KNOWLEDGE_STATS_SQL 一致：先按题反查，
        # 题查不到就用作答自带的 knowledge_id，否则智能解题页的作答又会整片消失。
        by_kp = conn.execute(
            "SELECT k.id AS knowledge_id, k.name, k.chapter, COUNT(*) AS attempts,"
            "SUM(CASE WHEN a.score>=60 THEN 1 ELSE 0 END) AS correct_n "
            f"FROM {scoped} a "
            "JOIN knowledge_points k ON k.id = COALESCE("
            "  (SELECT pk.knowledge_id FROM problem_knowledge pk "
            "   WHERE pk.problem_id = a.problem_id ORDER BY pk.weight DESC LIMIT 1),"
            "  NULLIF(a.knowledge_id, 0)) "
            f"WHERE {win} AND k.course_id = :course "
            "GROUP BY k.id ORDER BY attempts DESC, k.id", params,
        ).fetchall()
    return {
        "attempts": int(stats["attempts"] or 0),
        "correct": int(stats["correct"] or 0),
        "hinted_attempts": int(stats["hinted"] or 0),
        "duration_seconds": int(stats["duration"] or 0),
        "active_days": int(stats["active_days"] or 0),
        "by_knowledge": [dict(r) for r in by_kp],
    }


# 全班口径的窗口聚合。归属条件与 _COURSE_ATTEMPTS_SQL 一模一样（按题反查 或
# 作答自带的 knowledge_id），只是不再限定某一个人 —— 两处若用不同口径，
# 学生在周报里看到的"本周做了 12 题"和榜上的名次就会对不上。
_WINDOW_BOARD_SQL = """
SELECT a.username, COUNT(*) AS attempts,
       SUM(CASE WHEN a.score >= 60 THEN 1 ELSE 0 END) AS correct_n,
       COUNT(DISTINCT date(a.created_at)) AS active_days,
       SUM(COALESCE(a.duration, 0)) AS duration_seconds
FROM practice_attempts a
JOIN users u ON u.username = a.username AND u.role = 'student'
WHERE date(a.created_at) >= date(:start) AND date(a.created_at) < date(:end)
  AND (a.problem_id IN (SELECT id FROM problems WHERE course_id = :course)
       OR a.knowledge_id IN (SELECT id FROM knowledge_points WHERE course_id = :course))
GROUP BY a.username
"""


def window_board(course_id: str, start: str, end: str) -> list[dict]:
    """窗口内全体学生的作答聚合（周榜取数口）。"""
    with get_conn() as conn:
        rows = conn.execute(_WINDOW_BOARD_SQL,
                            {"course": course_id, "start": start, "end": end}).fetchall()
    return [dict(r) for r in rows]


def course_mastery_avg(course_id: str) -> dict[str, dict]:
    """{学生: {avg_mastery, points}}。只对有记录的知识点求平均 —— 与班级热力图同一条纪律。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.username, AVG(m.mastery) AS avg_mastery, COUNT(*) AS points "
            "FROM mastery_records m "
            "JOIN knowledge_points k ON k.id = m.knowledge_id "
            "JOIN users u ON u.username = m.username AND u.role='student' "
            "WHERE k.course_id = ? GROUP BY m.username", (course_id,)).fetchall()
    return {r["username"]: {"avg_mastery": round(float(r["avg_mastery"] or 0), 4),
                            "points": int(r["points"] or 0)} for r in rows}


def daily_counts(username: str, course_id: str, days: int = 14) -> list[dict]:
    """最近 days 天每天的作答量与正确数（学习态势那条折线）。只返回**有记录的天**，
    缺的日子由调用方补零 —— SQL 里造日期序列不值当，Python 里补更清楚。"""
    scoped = f"({_COURSE_ATTEMPTS_SQL})"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT date(a.created_at) AS day, COUNT(*) AS attempts, "
            f"SUM(CASE WHEN a.score>=60 THEN 1 ELSE 0 END) AS correct_n "
            f"FROM {scoped} a "
            f"WHERE date(a.created_at) >= date('now','localtime',:since) "
            f"GROUP BY day ORDER BY day",
            {"u": username, "course": course_id,
             "since": f"-{max(1, days) - 1} days"}).fetchall()
    return [dict(r) for r in rows]


def window_errors(username: str, course_id: str, start: str, end: str) -> dict:
    """窗口内新增 / 销账的错题，以及新增错题的错因分布（该生该门，非全班）。"""
    params = (username, course_id, start, end)
    join = ("FROM error_records e JOIN knowledge_points k ON k.id = e.knowledge_id "
            "WHERE e.username=? AND k.course_id=?")
    with get_conn() as conn:
        opened = conn.execute(
            f"SELECT COUNT(*) {join} AND date(e.created_at) >= date(?) "
            "AND date(e.created_at) < date(?)", params).fetchone()[0]
        # 只认有 resolved_at 的行：resolved_at 是后补的列，旧的已销账错题是 NULL，
        # 把它们算进任意一周都是无中生有。
        closed = conn.execute(
            f"SELECT COUNT(*) {join} AND e.resolved=1 AND e.resolved_at IS NOT NULL "
            "AND date(e.resolved_at) >= date(?) AND date(e.resolved_at) < date(?)",
            params).fetchone()[0]
        types = conn.execute(
            f"SELECT e.error_type, COUNT(*) AS cnt {join} AND date(e.created_at) >= date(?) "
            "AND date(e.created_at) < date(?) AND e.error_type != '' "
            "GROUP BY e.error_type ORDER BY cnt DESC, e.error_type", params).fetchall()
    return {"opened": int(opened), "closed": int(closed),
            "error_types": [dict(r) for r in types]}


def mastery_as_of(username: str, course_id: str, at: str) -> dict[int, float]:
    """{knowledge_id: 掌握度} —— 取每个知识点在 at 之前的最后一条快照。

    at 之前没有任何快照的知识点**不出现在返回值里**，与"快照值为 0"区分开：
    前者是没数据，后者是练过但掌握度为 0，对周报的措辞意味着完全不同的东西。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.knowledge_id, s.mastery FROM mastery_snapshots s "
            "JOIN knowledge_points k ON k.id = s.knowledge_id "
            "WHERE s.username=? AND k.course_id=? AND s.created_at < ? "
            "AND s.id = (SELECT MAX(s2.id) FROM mastery_snapshots s2 "
            "            WHERE s2.username=s.username AND s2.knowledge_id=s.knowledge_id "
            "              AND s2.created_at < ?)",
            (username, course_id, at, at)).fetchall()
    return {int(r["knowledge_id"]): float(r["mastery"] or 0.0) for r in rows}


def has_mastery_snapshots(username: str) -> bool:
    with get_conn() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM mastery_snapshots WHERE username=? LIMIT 1", (username,)).fetchone())


# ---------- 助教工作台：班级聚合 ----------
def aggregate_error_types(limit: int = 20, course_id: str = "", class_name: str = "") -> list[dict]:
    """学生错因聚合（共性错因），按频次降序。

    与 class_stats 同一个范围（I-03）：只算学生账号、按课、按班。此前这里只按课筛，
    助教切到某个班时，上面的"错题总数"是这个班的，下面的"共性错因"却是全体的，
    还混进了助教 / 演示账号自己的错题。
    注意统计的是 `error_type` 而不是 `ai_error_type`：助教改判后共性错因要反映
    纠正后的结果（见 _TABLE_EXTRA_COLS 里 error_records 那段注释）。
    """
    sql = ("SELECT e.error_type, COUNT(*) AS cnt FROM error_records e "
           "JOIN users u ON u.username = e.username ")
    where, params = ["u.role = 'student'"], []
    if course_id:
        sql += "JOIN knowledge_points k ON k.id = e.knowledge_id "
        where.append("k.course_id = ?")
        params.append(course_id)
    if class_name:
        where.append("COALESCE(u.class_name, '') = ?")
        params.append(class_name)
    sql += "WHERE " + " AND ".join(where) + " GROUP BY e.error_type ORDER BY cnt DESC LIMIT ?"
    with get_conn() as conn:
        rows = conn.execute(sql, [*params, limit]).fetchall()
    return [dict(r) for r in rows]


_CLASS_POINT_SQL = """
SELECT k.id AS knowledge_id, k.name, COALESCE(k.chapter, '') AS chapter,
       COUNT(DISTINCT m.username) AS learners,
       AVG(m.mastery) AS avg_mastery,
       (SELECT COUNT(*) FROM error_records e
        WHERE e.knowledge_id = k.id AND e.resolved = 0) AS error_open,
       (SELECT COUNT(*) FROM error_records e WHERE e.knowledge_id = k.id) AS error_total
FROM knowledge_points k
LEFT JOIN mastery_records m ON m.knowledge_id = k.id
WHERE k.course_id = ?
GROUP BY k.id
HAVING learners > 0 OR error_total > 0
ORDER BY k.id
"""


def class_points(course_id: str) -> list[dict]:
    """班级视角的知识点行：多少人练过、平均掌握度、未订正错题数。

    `HAVING` 把一次都没被碰过的知识点挡在外面：热力图上一整片"没有数据"的格子
    既占地方又会让人误读成"全班都不会"。没数据就是没数据，不画。
    """
    with get_conn() as conn:
        rows = conn.execute(_CLASS_POINT_SQL, (course_id,)).fetchall()
    return [dict(r) for r in rows]


def class_mastery_matrix(course_id: str, class_name: str = "") -> list[dict]:
    """(学生, 知识点) → 掌握度的稀疏表，只含真有记录的格子。"""
    sql = ("SELECT m.username, m.knowledge_id, m.mastery, m.state, m.last_practiced_at "
           "FROM mastery_records m "
           "JOIN knowledge_points k ON k.id = m.knowledge_id "
           "JOIN users u ON u.username = m.username "
           "WHERE k.course_id = ? AND u.role = 'student'")
    params: list = [course_id]
    if class_name:
        sql += " AND COALESCE(u.class_name, '') = ?"
        params.append(class_name)
    with get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY m.username, m.knowledge_id", params).fetchall()
    return [dict(r) for r in rows]


def list_students(class_name: str = "") -> list[dict]:
    sql = ("SELECT username, name, COALESCE(class_name,'') AS class_name, "
           "COALESCE(student_id,'') AS student_id FROM users WHERE role='student'")
    params: list = []
    if class_name:
        sql += " AND COALESCE(class_name,'') = ?"
        params.append(class_name)
    with get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY username", params).fetchall()
    return [dict(r) for r in rows]


def list_classes() -> list[str]:
    """有学生的班级名。空班级名（没填资料的账号）不算一个班。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT COALESCE(class_name,'') AS c FROM users "
            "WHERE role='student' AND COALESCE(class_name,'') <> '' ORDER BY c").fetchall()
    return [r["c"] for r in rows]


def latest_session(username: str, course_id: str, intent: str,
                   completed_only: bool = True) -> dict | None:
    """该生该门最近一条会话。`active_session` 找的是**没结束**的那条，
    这里要的是相反的一半：已经做完的最近一次（助教下钻要看他上次摸底考了什么）。"""
    sql = ("SELECT * FROM learning_sessions WHERE username=? AND course_id=? AND intent=? ")
    if completed_only:
        sql += "AND completed_at IS NOT NULL "
    sql += "ORDER BY id DESC LIMIT 1"
    with get_conn() as conn:
        row = conn.execute(sql, (username, course_id, intent)).fetchone()
    return dict(row) if row else None


def student_activity(course_id: str, class_name: str = "",
                     username: str = "") -> list[dict]:
    """每个学生在该门的作答量与正确数。热力图行首那几列用。

    `username` 只取一个人 —— 助教下钻一个学生时不该把全班扫一遍。"""
    sql = ("SELECT a.username, COUNT(*) AS attempts, "
           "SUM(CASE WHEN a.score >= 60 THEN 1 ELSE 0 END) AS correct_n, "
           "MAX(a.created_at) AS last_at "
           "FROM practice_attempts a "
           "JOIN users u ON u.username = a.username "
           f"WHERE {_ATTEMPT_IN_COURSE} AND u.role='student'")
    params: list = [course_id, course_id]
    if class_name:
        sql += " AND COALESCE(u.class_name,'') = ?"
        params.append(class_name)
    if username:
        sql += " AND a.username = ?"
        params.append(username)
    with get_conn() as conn:
        rows = conn.execute(sql + " GROUP BY a.username", params).fetchall()
    return [dict(r) for r in rows]


# ---------- 作业下发与验收 ----------
def create_assignment(ta_username: str, course_id: str, title: str, knowledge_ids: list[int],
                      *, note: str = "", target_count: int = 3, class_name: str = "",
                      due_date: str = "", source_error_type: str = "") -> int:
    import json as _json
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO assignments(ta_username,course_id,title,note,knowledge_ids,"
            "target_count,class_name,due_date,status,created_at,source_error_type)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ta_username, course_id, title, note,
             _json.dumps(sorted(set(int(k) for k in knowledge_ids))),
             int(target_count), class_name, due_date, "open", _now(), source_error_type))
        return cur.lastrowid


def _assignment_row(row) -> dict:
    import json as _json
    data = dict(row)
    try:
        data["knowledge_ids"] = _json.loads(data.get("knowledge_ids") or "[]")
    except (ValueError, TypeError):
        data["knowledge_ids"] = []
    return data


def get_assignment(assignment_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    return _assignment_row(row) if row else None


def list_assignments(course_id: str = "", *, open_only: bool = False,
                     class_name: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM assignments WHERE 1=1"
    params: list = []
    if course_id:
        sql += " AND course_id=?"
        params.append(course_id)
    if open_only:
        sql += " AND status='open'"
    if class_name is not None:
        # 面向全体的作业（class_name='')对每个班都可见，所以是"全体 或 本班"
        sql += " AND (class_name='' OR class_name=?)"
        params.append(class_name)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_assignment_row(r) for r in rows]


def close_assignment(assignment_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE assignments SET status='closed' WHERE id=? AND status='open'",
                           (assignment_id,))
        return cur.rowcount == 1


def assignment_effect(knowledge_ids: list[int], since: str, usernames: list[str]) -> dict:
    """作业的效果验证（docs/35 P1-7）：下发前后，目标知识点上发生了什么变化。

    两个口径，都只用真实记录：
    - 掌握度：每个 (学生, 知识点) 取下发时刻**之前最后一张快照**（mastery_snapshots，
      upsert_mastery 每次写都会追加）作为"前"，mastery_records 的当前值作为"后"。
      只统计前后都有值的配对 —— 下发前没测过的点没有"前"，硬补 0 会把涨幅吹大。
    - 作答正确率：下发之后在这些点上的作答，答对的占比。
    """
    empty = {"pairs": 0, "before": None, "after": None, "attempts_after": 0,
             "correct_after": 0, "accuracy_after": None, "students_active": 0}
    if not knowledge_ids or not usernames:
        return empty
    kh = ",".join("?" * len(knowledge_ids))
    uh = ",".join("?" * len(usernames))
    with get_conn() as conn:
        pairs = conn.execute(
            f"SELECT m.username, m.knowledge_id, m.mastery AS now_m, "
            f"  (SELECT s.mastery FROM mastery_snapshots s WHERE s.username = m.username "
            f"     AND s.knowledge_id = m.knowledge_id AND s.created_at < ? "
            f"   ORDER BY s.created_at DESC LIMIT 1) AS before_m "
            f"FROM mastery_records m WHERE m.knowledge_id IN ({kh}) AND m.username IN ({uh})",
            [since, *knowledge_ids, *usernames]).fetchall()
        att = conn.execute(
            f"SELECT COUNT(*) AS n, SUM(score >= 60) AS ok, COUNT(DISTINCT username) AS who "
            f"FROM practice_attempts WHERE knowledge_id IN ({kh}) AND username IN ({uh}) "
            f"AND created_at >= ?", [*knowledge_ids, *usernames, since]).fetchone()
    both = [(r["before_m"], r["now_m"]) for r in pairs if r["before_m"] is not None]
    out = dict(empty)
    if both:
        out.update(pairs=len(both),
                   before=round(sum(b for b, _ in both) / len(both), 4),
                   after=round(sum(a for _, a in both) / len(both), 4))
    n = int(att["n"] or 0)
    out.update(attempts_after=n, correct_after=int(att["ok"] or 0),
               accuracy_after=round((att["ok"] or 0) / n, 4) if n else None,
               students_active=int(att["who"] or 0))
    return out


def unreviewed_by_reason(course_id: str = "", class_name: str = "") -> dict[str, int]:
    """待复核错题按入队原因计数（助教「今日」首屏用）。"""
    where, params, join = ["COALESCE(e.ta_reviewed,0)=0"], [], ""
    if course_id:
        join += " JOIN knowledge_points k ON k.id = e.knowledge_id"
        where.append("k.course_id = ?")
        params.append(course_id)
    if class_name:
        join += " JOIN users u ON u.username = e.username"
        where.append("COALESCE(u.class_name,'') = ?")
        params.append(class_name)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT COALESCE(e.review_reason,'manual') AS r, COUNT(*) AS n FROM error_records e{join} "
            f"WHERE {' AND '.join(where)} GROUP BY 1", params).fetchall()
    return {r["r"]: int(r["n"]) for r in rows}


def assignment_done_matrix(knowledge_ids: list[int], since: str,
                           username: str = "") -> dict[str, dict[int, int]]:
    """{学生: {知识点: since 之后在该点上**答对**的题数}}。

    验收直接由作答算出来，不另存一份完成状态：多一份状态就多一处要跟作答对账，
    而两份数据迟早会不一致。代价是"作业完成度"永远等于"这段时间真的做对了几道"，
    这恰好也是助教真正想知道的那件事。

    **按知识点分开统计，不合并成一个总数。** 原先是 `COUNT(DISTINCT problem_id)`
    跨全部知识点算一个和，而下发表单、作业列表、学生首页三处文案都写着
    "每个知识点做对 N 题" —— 口径对不上：选 3 个知识点、target=3 时，
    学生在其中一个点上做对 3 道就算全部完成，另外两个点一道不碰。
    而助教选那 3 个点，要的正是"这三个薄弱点都得练到"。

    按题目去重（COUNT DISTINCT problem_id）：同一道题反复提交不该攒出完成度。
    副作用是智能解题页那条自由问答（problem_id 恒为 0）整场只算一道，
    这个方向的偏保守是对的 —— 作业要的是"做了几道不同的题"。
    """
    if not knowledge_ids:
        return {}
    holes = ",".join("?" * len(knowledge_ids))
    sql = (f"SELECT username, knowledge_id, COUNT(DISTINCT problem_id) AS done "
           f"FROM practice_attempts "
           f"WHERE knowledge_id IN ({holes}) AND created_at >= ? AND score >= 60")
    params: list = [*knowledge_ids, since]
    if username:
        sql += " AND username=?"
        params.append(username)
    out: dict[str, dict[int, int]] = {}
    with get_conn() as conn:
        for r in conn.execute(sql + " GROUP BY username, knowledge_id", params):
            out.setdefault(r["username"], {})[int(r["knowledge_id"])] = r["done"]
    return out


# 待复核的排序：AI 结论越站不住的越靠前（docs/35 P1-3）。学生主动申请排最前 ——
# 那是有人在等回复。
_REASON_ORDER = ("CASE COALESCE(e.review_reason,'manual') WHEN 'student_request' THEN 0 "
                 "WHEN 'verify_failed' THEN 1 WHEN 'no_evidence' THEN 2 WHEN 'careless' THEN 3 "
                 "ELSE 4 END")


def recent_errors(limit: int = 30, course_id: str = "", class_name: str = "",
                  reviewed: str = "", offset: int = 0, reason: str = "") -> tuple[list[dict], int]:
    """错题复核队列。返回 (这一页, 符合条件的总数)。

    原先这个函数是 `SELECT ... ORDER BY id DESC LIMIT ?`，**一个 WHERE 都没有**：
    助教只能看到全库最新的 15 条，看完就没了。想系统性复核一个班的错题 ——
    筛未复核的、只看自己带的那门课、翻到第二页 —— 一样都做不到。

    `reviewed` 取 "yes" / "no" / ""（全部）。默认按 id 倒序：**新的先复核**，
    因为错因诊断是要回流给学生看的，隔了两周的批注对他已经没用了。

    总数与这一页一起返回：没有总数的话前端只能显示"下一页"而不能显示"还有多少"，
    而助教要的正是"这个班还剩几条没复核"这个数。
    """
    where, params = ["1=1"], []
    join = ""
    if course_id:
        join += " JOIN knowledge_points k ON k.id = e.knowledge_id"
        where.append("k.course_id = ?")
        params.append(course_id)
    if class_name:
        join += " JOIN users u ON u.username = e.username"
        where.append("COALESCE(u.class_name,'') = ?")
        params.append(class_name)
    if reviewed == "yes":
        where.append("COALESCE(e.ta_reviewed, 0) = 1")
    elif reviewed == "no":
        where.append("COALESCE(e.ta_reviewed, 0) = 0")
    if reason:
        where.append("COALESCE(e.review_reason,'manual') = ?")
        params.append(reason)
    clause = f" FROM error_records e{join} WHERE " + " AND ".join(where)
    # 待复核按入队原因排，其余（已复核 / 全部）仍是新的在前
    order = f"{_REASON_ORDER}, e.id DESC" if reviewed == "no" else "e.id DESC"
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*)" + clause, params).fetchone()[0]
        # 复核要看的上下文：题干、学生作答、知识点、参考答案（docs/34 §3.6）。
        # 旧行没有 student_answer 列的值时，按 list_reviewed_errors 同一口径去
        # practice_attempts 找紧挨着的那次作答；找不到就留空，不编。
        rows = conn.execute(
            "SELECT e.id, e.username, e.error_type, e.diagnosis, e.created_at, "
            "e.ta_reviewed, e.ta_verdict, e.ta_note, e.ta_username, e.ta_reviewed_at, "
            "e.ai_error_type, e.knowledge_id, e.problem_id, COALESCE(e.tech_detail,'') AS tech_detail, "
            "COALESCE(e.review_reason,'manual') AS review_reason, e.review_requested_at, "
            "COALESCE(NULLIF(e.problem_stem,''), (SELECT p.stem FROM problems p WHERE p.id = e.problem_id), '') "
            "  AS problem_stem, "
            "(SELECT p.answer FROM problems p WHERE p.id = e.problem_id) AS reference, "
            "(SELECT kp.name FROM knowledge_points kp WHERE kp.id = e.knowledge_id) AS knowledge, "
            "COALESCE(NULLIF(e.student_answer,''), (SELECT a.answer FROM practice_attempts a "
            "  WHERE a.username = e.username AND a.problem_id = e.problem_id "
            "    AND a.problem_id > 0 AND a.created_at <= e.created_at "
            "  ORDER BY a.created_at DESC LIMIT 1), '') AS student_answer"
            + clause + f" ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, max(0, offset)]).fetchall()
    return [dict(r) for r in rows], total


def list_reviewed_errors(verdict: str = "", limit: int = 1000) -> list[dict]:
    """助教复核过的错题记录，附题干与该生当时的作答。

    偏好数据导出（scripts/build_preference_data.py）唯一的取数口。作答按
    (同一个人, 同一道题, 不晚于错题记录时间) 取最近的一条 —— 错题是在判题写回里
    紧接着作答落库的，这个口径能对上；对不上（旧数据、自由问答）就留空，
    由导出脚本决定丢弃，不在这里编。
    """
    sql = ("SELECT e.*, p.stem, p.answer AS reference, p.course_id, "
           "COALESCE(k.name, '') AS knowledge, "
           "(SELECT a.answer FROM practice_attempts a "
           " WHERE a.username = e.username AND a.problem_id = e.problem_id "
           "   AND a.problem_id > 0 AND a.created_at <= e.created_at "
           " ORDER BY a.created_at DESC LIMIT 1) AS attempt_answer "
           "FROM error_records e "
           "LEFT JOIN problems p ON p.id = e.problem_id "
           "LEFT JOIN knowledge_points k ON k.id = e.knowledge_id "
           "WHERE e.ta_reviewed = 1")
    params: list = []
    if verdict:
        sql += " AND e.ta_verdict = ?"
        params.append(verdict)
    sql += " ORDER BY e.id LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    # e.* 里现在自带 student_answer 列（新写入的行有值）；旧行为空时才用按时间对上的那次作答。
    # 子查询不能再叫 student_answer —— 同名列在 sqlite3.Row 里取到的是前一个（空串）。
    out = []
    for r in rows:
        d = dict(r)
        d["student_answer"] = d.get("student_answer") or d.pop("attempt_answer", None)
        d.pop("attempt_answer", None)
        out.append(d)
    return out


def get_error_record(record_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM error_records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def review_error_record(record_id: int, ta_username: str, verdict: str,
                        error_type: str = "", note: str = "") -> dict | None:
    """助教复核一条错题诊断："confirm" 只留痕；"override" 额外改写 error_type，
    让共性错因聚合和后续出题都看到纠正后的判定，同时把 AI 原判存进 ai_error_type 备查。
    """
    with get_conn() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM error_records WHERE id=?", (record_id,)).fetchone()
        if not row:
            return None
        if verdict == "override" and error_type and error_type != row["error_type"]:
            cur.execute(
                "UPDATE error_records SET error_type=?, ai_error_type=?, ta_reviewed=1, "
                "ta_verdict=?, ta_note=?, ta_username=?, ta_reviewed_at=? WHERE id=?",
                (error_type, row["ai_error_type"] or row["error_type"], verdict, note,
                 ta_username, _now(), record_id))
        else:
            cur.execute(
                "UPDATE error_records SET ta_reviewed=1, ta_verdict=?, ta_note=?, "
                "ta_username=?, ta_reviewed_at=? WHERE id=?",
                (verdict, note, ta_username, _now(), record_id))
        return dict(cur.execute("SELECT * FROM error_records WHERE id=?", (record_id,)).fetchone())


ACTIVE_WINDOW_DAYS = 7


def class_stats(course_id: str = "", class_name: str = "",
                active_days: int = ACTIVE_WINDOW_DAYS) -> dict:
    """总览那四个数。**按课、按班、按时间窗算**。

    原先是全库四个 COUNT，两处失真：四门课的作答混在一个数里，助教切到"计算机网络"
    看到的仍是四门之和；而 `active_students` 是"历史上做过任何一题的人"，
    开课两个月之后它等于总人数，永远满格，看不出这周还有几个人在学。

    `active_days` 是活跃窗口（默认 7 天）。返回里带上它，前端才能把
    "本周活跃"这句话写准 —— 写死在文案里的话，改了这里就对不上了。
    """
    joins, where, params = "", ["u.role='student'"], []
    if class_name:
        where.append("COALESCE(u.class_name,'') = ?")
        params.append(class_name)
    student_sql = "SELECT COUNT(*) FROM users u WHERE " + " AND ".join(where)

    # 作答与错题都要能按课过滤，而课的归属在知识点上，两张表都得 JOIN 过去。
    a_join = " JOIN users u ON u.username = a.username"
    a_where = list(where)
    a_params = list(params)
    if course_id:
        # 与学生端 _COURSE_ATTEMPTS_SQL 同一判据：题目属于这门课，或作答记在这门课的知识点上
        a_where.append(_ATTEMPT_IN_COURSE)
        a_params += [course_id, course_id]
    a_clause = f" FROM practice_attempts a{a_join} WHERE " + " AND ".join(a_where)

    e_join = " JOIN users u ON u.username = e.username"
    e_where = list(where)
    e_params = list(params)
    if course_id:
        e_join += " JOIN knowledge_points k ON k.id = e.knowledge_id"
        e_where.append("k.course_id = ?")
        e_params.append(course_id)
    e_clause = f" FROM error_records e{e_join} WHERE " + " AND ".join(e_where)

    # 用 time 而不是 datetime：这个模块全程只用 time（见 _now），
    # 为一个减法引第二套时间 API，下一个人就得先分辨两处时区语义是否一致。
    since = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(time.time() - max(1, active_days) * 86400))
    with get_conn() as conn:
        c = conn.cursor()
        students = c.execute(student_sql, params).fetchone()[0]
        attempts = c.execute("SELECT COUNT(*)" + a_clause, a_params).fetchone()[0]
        errors = c.execute("SELECT COUNT(*)" + e_clause, e_params).fetchone()[0]
        active = c.execute("SELECT COUNT(DISTINCT a.username)" + a_clause
                           + " AND a.created_at >= ?", [*a_params, since]).fetchone()[0]
    return {"students": students, "attempts": attempts, "errors": errors,
            "active_students": active, "active_days": max(1, active_days)}


# ---------------------------------------------------------------------------
# 资料入库任务
# ---------------------------------------------------------------------------
def create_ingest_job(filename: str, stored_path: str, course_id: str, doc_type: str,
                      parser: str, size_bytes: int, uploaded_by: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_jobs(filename,stored_path,course_id,doc_type,parser,"
            "status,size_bytes,uploaded_by,created_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
            (filename, stored_path, course_id, doc_type, parser, size_bytes,
             uploaded_by, _now()))
        return int(cur.lastrowid)


def update_ingest_job(job_id: int, **fields) -> None:
    """只允许改白名单里的列 —— 这些值来自后台任务，不该让它们决定拼进 SQL 的列名。"""
    allowed = ("status", "error", "parsed_chunks", "indexed_chunks",
               "duplicate_chunks", "finished_at")
    sets, args = [], []
    for k in allowed:
        if k in fields:
            sets.append(f"{k}=?")
            args.append(fields[k])
    if not sets:
        return
    args.append(job_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE ingest_jobs SET {','.join(sets)} WHERE id=?", args)


def list_ingest_jobs(course_id: str = "", limit: int = 50,
                     withdrawn: bool = False) -> list[dict]:
    """入库任务列表。默认**不含**已撤下的 —— 撤下的语义就是"从列表里消失"。

    但也不能让它彻底不可见：文件还在服务器上占着空间，教务需要一条路看到
    "撤下过什么"并把它放回去。所以是过滤而不是删行，withdrawn=True 单独查。
    """
    where, args = [], []
    if course_id:
        where.append("course_id=?")
        args.append(course_id)
    where.append("status=?" if withdrawn else "status<>?")
    args.append("withdrawn")
    sql = f"SELECT * FROM ingest_jobs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_ingest_job(job_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ingest_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def withdraw_ingest_job(job_id: int, parked_path: str, origin_path: str) -> None:
    """把任务行标成已撤下，并记下文件被挪到了哪、原先在哪。

    不删行：删了就无从知道这份资料曾经存在过，而文件还在服务器上占着空间 ——
    "盘上有一堆没人认识的文件"是比多一行记录糟得多的状态。
    """
    with get_conn() as conn:
        conn.execute("UPDATE ingest_jobs SET status='withdrawn', withdrawn_at=?, "
                     "stored_path=?, origin_path=? WHERE id=?",
                     (_now(), parked_path, origin_path, job_id))


def restore_ingest_job(job_id: int, stored_path: str) -> None:
    """放回去：状态回到 pending，等后台重新解析入库。"""
    with get_conn() as conn:
        conn.execute("UPDATE ingest_jobs SET status='pending', withdrawn_at=NULL, "
                     "origin_path='', error='', parsed_chunks=0, indexed_chunks=0, "
                     "duplicate_chunks=0, finished_at=NULL, stored_path=? WHERE id=?",
                     (stored_path, job_id))
