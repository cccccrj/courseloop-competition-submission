"""TutorGraph：追问—纠错—再讲解 状态机（命题核心闭环）。

设计成「框架无关」的会话状态机（后续可用 LangGraph 包裹）。分级提示不一次给全解：
    L1 提示知识点 -> L2 指出策略/方向 -> L3 展示局部步骤 -> L4 完整解析 -> L5 变式题验证
学生作答后走：诊断 -> 首错定位 + 错因 -> 换讲法 + 最小纠正 -> 再验证 -> 更新掌握度/错题。

会话状态进程内保存（单进程演示足够；生产可换 Redis/DB）。关键结果落库 error_records/mastery。
"""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from backend.agents import solver as solver_mod
from backend.agents import error_diagnoser as diag_mod
from backend.agents import practice_generator as pg_mod
from backend.core import db
from backend.core.config import settings
from backend.core.courses import get_course
from backend.learning import mastery as mastery_mod
from backend.learning.error_taxonomy import enforce_careless_gate, name_of, review_reason_for

STATES = ["await_idea", "hinting", "diagnosed", "resolved", "variant"]
MAX_LEVEL = 5
NEXT_TIP = "修改你的步骤后再次提交，或点『要更多提示』获取分级引导。"


@dataclass
class Session:
    id: str
    username: str
    problem: str
    context: str = ""                 # RAG 教材片段
    course: str = "ds-algo"           # 所属科目（408 四门之一）
    state: str = "await_idea"
    hint_level: int = 0
    hints_used: int = 0
    attempts: int = 0
    solution: dict | None = None      # 缓存 solver 结果，用于分级提示
    error_tags: list[str] = field(default_factory=list)
    first_wrong_step: int = 0
    knowledge: str = ""
    mastery: float = 0.0
    db_session_id: int = 0             # learning_sessions.id，用于对话留痕
    origin_problem: str = ""          # 切到变式题之后，原题留在这里供回看


# 会话状态在进程内存里。原先 id 是全局自增计数器（s00001、s00002…），有两个后果：
#   1. 可猜 —— 知道 id 就能驱动别人的会话，而 submit/hint/variant 原先不校验身份；
#   2. 服务一重启就从 s00001 重新发号，还揣着旧 id 的前端下一次提交会命中**别人**的新会话。
# 换成 uuid4：既不可猜，也不会重号。
_SESSIONS: dict[str, Session] = {}

# 进程内的字典只增不减，长时间演示会一直涨。超过上限就按插入顺序丢最旧的
# （Python 3.7+ dict 保序），丢掉的只是内存态，关键结果早已落库。
MAX_SESSIONS = 500


def _new_id() -> str:
    return uuid.uuid4().hex


# 快照里存哪些字段。id / username / db_session_id 单独存在表的列上，这里只列
# 状态机自己的那几个数 —— 加字段时改这一处，存与取自然对称。
_SNAPSHOT_FIELDS = ("problem", "context", "course", "state", "hint_level", "hints_used",
                    "attempts", "solution", "error_tags", "first_wrong_step",
                    "knowledge", "mastery", "origin_problem")


def _save_snapshot(sess: Session) -> None:
    """把会话状态写回库。任何一步失败都只是"这次不能续"，不该打断正在进行的解题。"""
    if not sess.db_session_id or not sess.username:
        return
    try:
        data = {f: getattr(sess, f) for f in _SNAPSHOT_FIELDS}
        db.save_tutor_snapshot(sess.db_session_id, sess.id, sess.username,
                               _json.dumps(data, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def _save_tutor_turn(sess: Session, content: str, data: dict) -> None:
    """写一条助教轮次。落库失败不影响这次交互本身。"""
    if not sess.db_session_id:
        return
    try:
        db.save_turn(sess.db_session_id, "assistant", content,
                     structured_data=_json.dumps(data, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def _restore(session_id: str = "", db_session_id: int = 0, username: str = "") -> Session | None:
    """从快照重建会话并放回内存。归属不符一律当作不存在。"""
    try:
        row = db.get_tutor_snapshot(session_id=session_id, db_session_id=db_session_id)
    except Exception:  # noqa: BLE001
        return None
    if not row or row.get("username") != (username or ""):
        return None
    try:
        data = _json.loads(row.get("snapshot") or "{}")
    except (ValueError, TypeError):
        return None
    sess = Session(id=row["session_id"], username=row["username"],
                   problem=data.get("problem", ""), db_session_id=int(row["db_session_id"]))
    for f in _SNAPSHOT_FIELDS:
        if f in data and f != "problem":
            setattr(sess, f, data[f])
    sess.origin_problem = sess.origin_problem or ""   # 旧快照没有这一项
    _SESSIONS[sess.id] = sess
    return sess


logger = logging.getLogger(__name__)

# 解析预热。分级提示、参考答案都要先有整题的结构化解析（solver），原先是学生第一次点
# 「要更多提示」时才去生成 —— 真实模型下这一下要等 20 多秒（docs/36 §5 实测 23.8s），
# 演示时最显眼的一处卡顿。现在开始解题时就在后台先算：学生写思路的那半分钟正好够它算完。
# 同一个会话只会算一次：_ensure_solution 先等后台那份，不另起一次调用。
_PREFETCH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="solve-prefetch")
_PREFETCH: dict[str, Future] = {}
_PREFETCH_LOCK = threading.Lock()
PREFETCH_WAIT_S = 180


def _compute_solution(sess: Session, request: tuple[str, str, str] | None = None) -> None:
    # 在线程入队时固定请求，不在工作线程真正开始时读取可变的题目。
    with _PREFETCH_LOCK:
        problem, context, course = request or (sess.problem, sess.context, sess.course)
        if sess.problem != problem or sess.solution is not None:
            return
    sol = solver_mod.solve(problem, context=context, course=course)
    # 与 variant 的切题操作共用锁：检查和写回不可被切题穿插。
    with _PREFETCH_LOCK:
        if sess.problem != problem or sess.solution is not None:
            return
        sess.solution = sol
        kps = sol.get("knowledge_points") or []
        sess.knowledge = kps[0] if kps else ""
        _save_snapshot(sess)


def _prefetch_solution(sess: Session) -> None:
    """后台预热这道题的解析。没配文本模型就不做（solver 用不了，测试里也不会意外打真实接口）。"""
    if not settings.text_llm_ready or sess.solution is not None:
        return
    request = (sess.problem, sess.context, sess.course)

    def job() -> None:
        try:
            _compute_solution(sess, request)
        except Exception:  # noqa: BLE001 —— 预热失败不要紧，用到时会同步再算一次
            logger.exception("解析预热失败 session=%s", sess.id)

    with _PREFETCH_LOCK:
        if sess.id not in _PREFETCH:
            _PREFETCH[sess.id] = _PREFETCH_POOL.submit(job)


def _ensure_solution(sess: Session) -> dict:
    with _PREFETCH_LOCK:
        fut = _PREFETCH.pop(sess.id, None)
    if fut is not None:
        try:
            fut.result(timeout=PREFETCH_WAIT_S)   # 预热还在跑就等它，不重复调用模型
        except Exception:  # noqa: BLE001
            pass
    if sess.solution is None:
        _compute_solution(sess)
    return sess.solution


def start(username: str, problem: str, context: str = "", course: str = "ds-algo") -> dict:
    sess = Session(id=_new_id(), username=username, problem=problem, context=context, course=course)
    if username:
        try:
            # 带上 course_id：不传会吃 create_session 的默认值 ds-algo，
            # 于是计组/OS/网络的会话在库里全被记成数据结构。
            sess.db_session_id = db.create_session(username, course_id=course, intent="tutor")
            db.save_turn(sess.db_session_id, "student", problem)
        except Exception:  # noqa: BLE001
            pass
    _SESSIONS[sess.id] = sess
    while len(_SESSIONS) > MAX_SESSIONS:
        _SESSIONS.pop(next(iter(_SESSIONS)), None)
    _save_snapshot(sess)
    _prefetch_solution(sess)
    return {"session_id": sess.id, "state": sess.state, "message": opener(course),
            "db_session_id": sess.db_session_id}


def opener(course: str) -> str:
    """开场白。start 与 resume 必须是同一句：读回的会话开头换了说法，
    看起来就像是另一次对话。"""
    return ("先说说你的思路：你打算用什么数据结构 / 算法？卡在哪一步？"
            if get_course(course).code_enabled
            else "先说说你的思路：用到哪些定义/公式？打算怎么算？卡在哪一步？")


def get(session_id: str) -> Session | None:
    return _SESSIONS.get(session_id) or _restore(session_id=session_id)


def get_owned(session_id: str, username: str) -> Session | None:
    """取会话，并校验调用方就是发起人。不是本人一律当作「会话不存在」。

    uuid 已经让 id 猜不到，但"猜不到"和"拿到了也用不了"是两回事 ——
    id 会经前端、日志、截图流出去，所有权检查才是真正的边界。
    匿名会话（username=""）只有同样匿名的调用方能续，避免登录用户接管它。
    """
    sess = _SESSIONS.get(session_id)
    if sess is None:
        # 内存里没有不等于会话没了：后端重启过，或者它被 MAX_SESSIONS 挤了出去。
        # 快照里带着 username，_restore 自己会做归属校验。
        return _restore(session_id=session_id, username=username)
    if sess.username != (username or ""):
        return None
    return sess


def _hint_for_level(sess: Session, level: int) -> dict:
    """按分级给出提示，内容取自缓存的结构化解析（不一次给全解）。"""
    sol = _ensure_solution(sess)
    steps = sol.get("steps", [])
    if level <= 1:
        return {"level": 1, "title": "提示知识点",
                "content": "涉及知识点：" + "、".join(sol.get("knowledge_points", [])) +
                           ("；先回忆它们的定义与适用条件。" if sol.get("knowledge_points") else "")}
    if level == 2:
        return {"level": 2, "title": "指出方向", "content": "解题策略：" + (sol.get("strategy") or "")}
    if level == 3:
        if not steps:
            # 变式题没有逐步解析（出题器只给答案与提示），第 3 级退回到方向提示
            return {"level": 3, "title": "展示局部步骤",
                    "content": "先按这个方向写出第一步：" + (sol.get("strategy") or "回到题目条件，逐条列出已知量。")}
        first = steps[0]
        return {"level": 3, "title": "展示局部步骤",
                "content": f"第1步：{first.get('action','')}（依据：{first.get('basis','')}）"}
    if level == 4:
        lines = [f"{s.get('index')}. {s.get('action')}（依据：{s.get('basis')}）" for s in steps]
        return {"level": 4, "title": "完整步骤化解析",
                "content": "\n".join(lines) + f"\n\n最终答案：{sol.get('final_answer','')}",
                "solution": sol}
    return {"level": 5, "title": "变式题验证", "content": "来做一道同类变式题，检验是否真正掌握。"}


def hint(session_id: str, username: str = "") -> dict:
    sess = get_owned(session_id, username)
    if not sess:
        return {"error": "会话不存在"}
    sess.hint_level = min(sess.hint_level + 1, MAX_LEVEL)
    sess.hints_used += 1
    sess.state = "hinting"
    h = _hint_for_level(sess, sess.hint_level)
    _save_snapshot(sess)
    # 提示与变式题以前不落库，只有诊断落。于是读回一次会话时，学生看到的是
    # 「我答了 → 助教诊断」，中间那几级提示凭空消失，对不上他当时的思路。
    _save_tutor_turn(sess, h.get("content", ""), {"kind": "hint", **h})
    return {"session_id": session_id, "state": sess.state, "hint_level": sess.hint_level,
            "max_level": MAX_LEVEL, "hint": h}


def ensure_solution(sess: Session) -> dict:
    """公开包装：确保已生成结构化解析（供流式端点取参考答案/知识点）。"""
    return _ensure_solution(sess)


def apply_diagnosis(sess: Session, student_answer: str, d: dict, student_code: str = "") -> dict:
    """把诊断结果落到会话：判定对错(沙箱优先)、更新掌握度、落库、返回前端负载。"""
    if sess.db_session_id and (student_answer or student_code):
        try:
            db.save_turn(sess.db_session_id, "student", student_answer or student_code)
        except Exception:  # noqa: BLE001
            pass
    correct = bool(d.get("correct"))
    ver = d.get("verification")
    if ver and ver.get("passed") is not None:
        correct = bool(ver["passed"])
    # 代码运行失败的原始报错只进错题的 tech_detail（助教复核可展开），不随响应发给页面（I-05）
    tech = ""
    if ver and isinstance(ver.get("extra"), dict) and "tech" in ver["extra"]:
        ver = {**ver, "extra": {k: v for k, v in ver["extra"].items() if k != "tech"}}
        tech = d["verification"]["extra"]["tech"] or ""

    sess.error_tags = d.get("error_tags", [])
    sess.first_wrong_step = d.get("first_wrong_step", 0)

    is_mis = "concept_confuse" in sess.error_tags
    sess.mastery = mastery_mod.update_mastery(sess.mastery, correct, difficulty=2,
                                              hints_used=sess.hints_used, is_misconception=is_mis)
    state_label = mastery_mod.derive_state(sess.mastery, sess.attempts, correct, is_mis and not correct)
    error_record_id = 0   # 学生在诊断卡上点"请助教看看"时，要标的就是这一条
    if sess.username:
        try:
            with db.transaction():
                kid = db.get_or_create_knowledge_id(sess.knowledge, sess.course)
                # 会话值可能来自另一台设备的旧快照；以事务内数据库值为准累积。
                previous = db.get_mastery(sess.username, kid)
                if previous:
                    try:
                        days = max(0, (datetime.now() - datetime.strptime(
                            previous["last_practiced_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()/86400)
                    except (ValueError, TypeError):
                        days = 0
                    base = mastery_mod.apply_forgetting(previous["mastery"], days)
                else:
                    base = 0.0
                sess.mastery = mastery_mod.update_mastery(base, correct, difficulty=2,
                    hints_used=sess.hints_used, is_misconception=is_mis)
                n_att = db.count_attempts(sess.username, kid) + 1
                state_label = mastery_mod.derive_state(sess.mastery, n_att, correct, is_mis and not correct)
                if not correct and sess.error_tags[:1] == ["careless"]:
                    # 必须在写这次作答之前查：这次本身是错的，查"之前做对过没有"
                    sess.error_tags = enforce_careless_gate(sess.error_tags,
                                                            db.had_correct_attempt(sess.username, kid))
                    if sess.error_tags[:1] != ["careless"]:
                        d = {**d, "error_tags": sess.error_tags,
                             "error_tag_names": [name_of(t) for t in sess.error_tags]}
                # 带上 knowledge_id / session_id：这里的题目是自由输入的，没有 problems.id，
                # 只按 problem_id 落库的话，这条作答在薄弱画像与学习中心里都不存在
                # （db._KNOWLEDGE_STATS_SQL 曾经按 problem_id > 0 过滤掉它们）。
                db.save_attempt(sess.username, student_answer or student_code,
                                float(d.get("score", 100 if correct else 0)), sess.hints_used,
                                knowledge_id=kid, session_id=sess.db_session_id)
                db.upsert_mastery(sess.username, kid, sess.mastery,
                                  mastery_mod.confidence(n_att), state_label)
                if not correct:
                    err = sess.error_tags[0] if sess.error_tags else "unknown"
                    error_record_id = db.save_error_record(sess.username,
                                         error_type=err,
                                         diagnosis=d.get("diagnosis", ""),
                                         knowledge=sess.knowledge, problem_stem=sess.problem,
                                         knowledge_id=kid,
                                         student_answer=student_code or student_answer,
                                         review_reason=review_reason_for(
                                             err, d.get("verification"), d.get("_has_evidence")),
                                         tech_detail=tech)
                db.save_turn(sess.db_session_id, "assistant", d.get("diagnosis", "") or "作答正确",
                             structured_data=_json.dumps(
                                 {**d, "verification": ver,   # 已去掉 tech，/sessions 会原样读回
                                  "kind": "correct" if correct else "diagnosis",
                                  "next": NEXT_TIP}, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    if correct:
        sess.state = "resolved"
        _save_snapshot(sess)
        return {"session_id": sess.id, "state": sess.state, "correct": True,
                "message": "答对了。可以再做一道变式题，确认真的掌握了。",
                "mastery": sess.mastery, "mastery_state": state_label, "diagnosis": d}

    sess.state = "diagnosed"
    sess.hint_level = max(sess.hint_level, 1)
    _save_snapshot(sess)
    return {"session_id": sess.id, "state": sess.state, "correct": False,
            "first_wrong_step": sess.first_wrong_step,
            "error_tags": d.get("error_tags", []), "error_tag_names": d.get("error_tag_names", []),
            "diagnosis": d.get("diagnosis", ""), "remediation": d.get("remediation", ""),
            "reexplain": d.get("reexplain", ""), "verification": ver,
            "error_record_id": error_record_id,
            "mastery": sess.mastery, "mastery_state": state_label,
            "next": NEXT_TIP}


def submit(session_id: str, student_answer: str, student_code: str = "",
           func_name: str = "", tests: list[dict] | None = None,
           username: str = "") -> dict:
    """非流式：学生作答 -> 诊断 -> 更新掌握度与错题。"""
    sess = get_owned(session_id, username)
    if not sess:
        return {"error": "会话不存在"}
    sess.attempts += 1
    ref = _ensure_solution(sess).get("final_answer", "")
    d = diag_mod.diagnose(sess.problem, student_answer, reference=ref,
                          student_code=student_code, func_name=func_name, tests=tests,
                          course=sess.course)
    return apply_diagnosis(sess, student_answer, d, student_code=student_code)


def _replay_turns(db_session_id: int) -> list[dict]:
    """把落库的轮次翻译回前端那套 Turn。

    第一条学生轮存的是题目本身（start 写的），不是一次作答 —— 它已经在
    「当前题目」那一栏里显示了，再放进对话流会变成学生自言自语地把题抄了一遍。
    """
    out: list[dict] = []
    seen_problem = False
    for t in db.list_turns(db_session_id):
        role, content = t.get("role"), t.get("content") or ""
        if role == "student":
            if not seen_problem:
                seen_problem = True
                continue
            out.append({"role": "student", "text": content})
            continue
        if role != "assistant":
            continue
        try:
            data = _json.loads(t.get("structured_data") or "{}")
        except (ValueError, TypeError):
            data = {}
        kind = data.get("kind")
        if not kind:
            # 快照机制上线前落的诊断轮次没有 kind，按内容特征认回来。
            kind = "diagnosis" if ("error_tags" in data or "first_wrong_step" in data) else "correct"
        if kind in ("correct", "ask"):
            out.append({"role": "tutor", "kind": kind, "text": content})
        else:
            out.append({"role": "tutor", "kind": kind, "data": data})
    return out


def resume(username: str, course: str = "ds-algo", db_session_id: int = 0) -> dict:
    """读回一次解题会话：状态机 + 完整对话流。

    不传 db_session_id 就取该生该门最近一次未结束的解题会话 —— 刷新、换设备、
    后端重启之后回到解题页，看到的仍是同一道题、同一段对话、同一级提示。
    没有可续的会话时返回 {}（不是 404：「没有历史」是正常状态，不是错误）。
    """
    if not username:
        return {}
    if not db_session_id:
        row = db.active_session(username, course, "tutor")
        if not row:
            return {}
        db_session_id = int(row["id"])
    sess = _restore(db_session_id=db_session_id, username=username)
    if sess is None:
        return {}
    turns = [{"role": "tutor", "kind": "ask", "text": opener(sess.course)}]
    turns.extend(_replay_turns(db_session_id))
    return {"session_id": sess.id, "db_session_id": db_session_id, "problem": sess.problem,
            "course": sess.course, "state": sess.state, "hint_level": sess.hint_level,
            "mastery": sess.mastery, "resolved": sess.state == "resolved",
            "origin_problem": sess.origin_problem, "turns": turns}


def variant(session_id: str, username: str = "") -> dict:
    """生成针对本题错因的变式题（先验证后展示），并**把会话切到这道变式题上**。

    此前只把状态置成 variant、题目没换：学生接着在同一个输入框作答，诊断仍拿原题
    判分 —— "变式练习 → 复测验证"这两环在解题页里是断的（docs/34 §3.2）。
    现在变式题成为会话的当前题目：参考答案用出题器自带、已过自检的那份，
    提示级别与错因清零（这是一道新题），原题留在 origin_problem 里供回看。
    """
    sess = get_owned(session_id, username)
    if not sess:
        return {"error": "会话不存在"}
    err = sess.error_tags[0] if sess.error_tags else ""
    p = pg_mod.generate(sess.knowledge or get_course(sess.course).short, context=sess.context,
                        error_type=err, difficulty=2, course=sess.course)
    stem = (p.get("stem") or "").strip()
    if not stem:
        # 出题失败就别切题：切过去一道空题，学生答什么都没法判
        return {"session_id": session_id, "state": sess.state, "problem": p,
                "error": "这次没能生成变式题，请稍后再试。"}
    with _PREFETCH_LOCK:
        _PREFETCH.pop(sess.id, None)   # 原题的预热若还在跑，结果不再需要
        if not sess.origin_problem:
            sess.origin_problem = sess.problem
        sess.problem = stem
        sess.solution = {
            "final_answer": p.get("answer") or "",
            "knowledge_points": [sess.knowledge] if sess.knowledge else [],
            "strategy": p.get("hint") or "",
            "steps": [],
        }
        sess.hint_level = 0
        sess.error_tags = []
        sess.first_wrong_step = 0
        sess.state = "variant"
        _save_snapshot(sess)
    _save_tutor_turn(sess, stem, {"kind": "variant", **p})
    return {"session_id": session_id, "state": sess.state, "problem": p,
            "current_problem": sess.problem, "origin_problem": sess.origin_problem}
