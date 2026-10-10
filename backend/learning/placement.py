"""入学摸底 / 自适应分级测试 —— 冷启动的解法。

## 它补的是哪个洞

整个系统由作答数据驱动：掌握度、薄弱画像、练习推荐、复习排期，全都要先有作答。
新账号打开就是空的，`cold_start` 只是一个提示 flag，没有任何机制去解决它 ——
学习中心、练习页、复习计划三处各自显示一句"还没有数据"，然后就没有下文了。
摸底做的事很直接：十来道题，把这个人的水平量出来，给测到的知识点写上初值，
让下游那一整条链路立刻有东西可算。

## 三条设计纪律

**1. 只用可确定性判定的客观题。** 题池见 `db.placement_pool`。摸底要一题接一题
当场判、当场决定下一题出多难，中间不能有一次模型调用 —— 十几题各等一次 LLM，
这个功能就没法用了。而且分级结论要可复现：同样的作答必须给出同样的档位。

**2. 只给真正测到的知识点写初值，不外推。** 116 个知识点，摸底顶多覆盖十来个。
把章节水平摊给同章未测的点，写进去的就是编的数 —— 而 `mastery_records` 是薄弱
排序、错题销账、复习计划的共同输入，编一处脏一路。未测到的点保持 not_started，
结果页如实报覆盖率。同理，**已经有掌握度记录的知识点一律跳过**：真实作答攒出来的
估计比一道摸底题可靠，摸底不该把它冲掉。

**3. 难度分档与 profile 共用一把尺子。** 摸底的直接产物是"这个人能站住第几档难度"，
而 `profile._DIFFICULTY_BANDS` 本来就是"掌握度 → 下一题该出多难"的映射。
把它反过来用（见 `mastery_for_level`），摸底写下的初值和练习页取的目标难度天然自洽；
另立一张表就会出现"摸底说你是进阶、练习页却一直推最简单的题"这种没法解释的不一致。

## 会话状态在库里，不在内存里

每问一题、每答一题都记进 `dialog_turns`，会话本身是 `learning_sessions` 的一行
（intent='placement'）。当前难度、已覆盖章节、答到第几题，全部由重放这些轮次得出
（`_replay`），所以刷新页面、换个设备都能接着答，服务重启也不丢 ——
这正是 TutorGraph 那套进程内会话踩过的坑。顺带，`dialog_turns` 从建表起只写不读，
这里是它第一个消费方。
"""
from __future__ import annotations

import json

from backend.core import db
from backend.core.courses import get_course
from backend.learning import mastery as mastery_mod
from backend.learning import profile as profile_mod
from backend.learning.error_taxonomy import error_type_for_canonical
from backend.verification.numeric_checker import canonical_text, check_numeric

INTENT = "placement"

DEFAULT_LENGTH = 12
MIN_LENGTH = 6          # 少于这个题数，覆盖不到几个章节，"分级"就名不副实
MAX_LENGTH = 20
START_DIFFICULTY = 3    # 从中间档起步：先不假设这个人强还是弱

# 档位的说法。0 档是"连第 1 档都没站住"，它需要一个不伤人的名字 ——
# 摸底多半是新用户进来的第一个页面，第一句话不该是"你不行"。
LEVEL_NAMES = {
    0: "起步", 1: "入门", 2: "基础", 3: "进阶", 4: "熟练", 5: "精通",
}
LEVEL_HINTS = {
    0: "先从每章最基础的定义和例题开始，不必急着刷综合题。",
    1: "基本概念还需要过一遍，建议按章节顺序补，边看边做简单题。",
    2: "常规题型已经能应付，接下来重点练中等难度和易混概念。",
    3: "主干知识站得住，差在综合应用和计算细节上。",
    4: "整体扎实，把剩下的薄弱章节补齐就能上强度。",
    5: "基础很好，建议直接进真题和综合应用题。",
}


# ---------- 档位 ↔ 掌握度：与 profile 共用同一张难度表 ----------
def _band_bounds() -> list[float]:
    """把 profile._DIFFICULTY_BANDS 展开成 6 个边界点：[0, .30, .50, .70, .85, 1]。

    从那份数据算出来而不是抄一份常量：难度分档以后要是调了，这里跟着变，
    不会出现"练习页按新表出题、摸底按旧表定级"。
    """
    return [0.0] + [ceiling for ceiling, _ in profile_mod._DIFFICULTY_BANDS] + [1.0]


def mastery_for_level(level: int) -> float:
    """「能站住第 level 档难度」翻译回掌握度：取该档在难度表上对应区间的中点。

    反解而不是另定一套分数：`profile._target_difficulty(mastery_for_level(L)) == L`
    对 1~5 都成立（见 tests/test_placement.py），也就是摸底写下初值之后，
    练习页第一时间推给这个人的题，难度恰好就是摸底测出来的那一档。
    0 档没有对应区间，给一个明显低于第 1 档中点的值。
    """
    if level <= 0:
        return 0.05
    bounds = _band_bounds()
    level = min(level, len(bounds) - 1)
    return round((bounds[level - 1] + bounds[level]) / 2, 4)


def level_of(items: list[dict]) -> int | None:
    """一组已判定的作答 → 能站住的难度档（0~5）。空列表返回 None（没测到，不是 0 分）。

    做对过的最高一档就是结论，但那一档的正确率要过半，免得一次蒙对定档。
    单题一档时"过半"等于"这题对了" —— 看着松，其实有自适应本身兜着：
    难度是答对才往上走的，没有连续做对根本见不到高档题。何况摸底写下的
    confidence 只有一道题的分量（`mastery.confidence(1) ≈ 0.28`），
    后面真练几道就会把它顶掉。

    一档都没站住时退回「做错的最低档 - 1」而不是直接判 0 ——
    第 3 档做错只否定第 3 档，并不说明第 1、2 档也不会。这条对结果影响很大：
    按 0 处理的话，绝大多数人摸完底都会被写成接近零的掌握度，
    下游推荐随即退化成"从头开始"，摸底也就白做了。
    """
    if not items:
        return None
    for level in (5, 4, 3, 2, 1):
        sub = [i for i in items if i["difficulty"] == level]
        if sub and sum(1 for i in sub if i["correct"]) / len(sub) >= 0.5:
            return level
    return max(0, min(i["difficulty"] for i in items) - 1)


# ---------- 会话状态：由 dialog_turns 重放得出 ----------
def _loads(raw: str | None) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _replay(session_id: int) -> tuple[dict, list[dict]]:
    """重放会话轮次 → (会话元信息, 题目列表)。纯读。

    题目按 seq 对齐：assistant 轮记"出了哪道题"，student 轮记"答成什么样"。
    两者靠 seq 配对而不是靠相邻位置，免得中间多出一轮就整体错位。
    """
    meta: dict = {"length": DEFAULT_LENGTH}
    items: dict[int, dict] = {}
    for turn in db.list_turns(session_id):
        data = _loads(turn["structured_data"])
        seq = int(data.get("seq") or 0)
        if turn["role"] == "system":
            meta.update(data)
        elif turn["role"] == "assistant" and seq:
            items[seq] = {"seq": seq, "problem_id": int(data.get("problem_id") or 0),
                          "difficulty": int(data.get("difficulty") or 2),
                          "knowledge_id": int(data.get("knowledge_id") or 0),
                          "knowledge": data.get("knowledge") or "",
                          "chapter": data.get("chapter") or "",
                          "target": int(data.get("target") or START_DIFFICULTY),
                          "answered": False, "correct": None, "answer": "",
                          "details": "", "duration": 0}
        elif turn["role"] == "student" and seq in items:
            items[seq].update(answered=True, correct=data.get("correct"),
                              answer=turn["content"], details=data.get("details") or "",
                              duration=int(data.get("duration") or 0))
    return meta, [items[k] for k in sorted(items)]


def _next_target(items: list[dict]) -> int:
    """当前该出多难：答对上一档、答错下一档，判不了的不动。

    "判不了"（check_numeric 返回 None，作答无法解析）既不是对也不是错，
    拿它调难度等于凭噪声改题 —— 验证器的三态在这里也要守住。
    """
    target = START_DIFFICULTY
    for item in items:
        if not item["answered"] or item["correct"] is None:
            continue
        target = max(1, min(5, target + (1 if item["correct"] else -1)))
    return target


def _pick(pool: list[dict], asked: set[int], chapter_counts: dict[str, int],
          target: int) -> dict | None:
    """选下一题：先补章节覆盖，再挑没做过的，最后才看难度是否贴着目标。

    章节覆盖排在难度前面，是因为摸底的产出是"哪几章弱"，而不是一个总分 ——
    十二道题全押在最弱那一章上，分级会更准，但这份报告就没法告诉人该先补哪。
    排序完全确定：同一份题库与同一份作答记录，永远给出同一套题。
    """
    candidates = [p for p in pool if p["problem_id"] not in asked]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (chapter_counts.get(p["chapter"], 0),
                                          p["seen"], abs(p["difficulty"] - target),
                                          p["problem_id"]))


def _item_payload(problem_id: int, seq: int, total: int, chapter: str) -> dict:
    """发给前端的题面。答案形态（answer_kind / unit / multi）直接取 db.get_problem，
    它已经算好了这几个字段且不泄露答案值，前端据此渲染选项框或数值框。"""
    full = db.get_problem(problem_id) or {}
    return {"seq": seq, "total": total, "problem_id": problem_id,
            "stem": full.get("stem", ""), "type": full.get("type", ""),
            "difficulty": full.get("difficulty", 2), "knowledge": full.get("knowledge", ""),
            "chapter": chapter, "answer_kind": full.get("answer_kind", ""),
            "answer_unit": full.get("answer_unit", ""),
            "answer_multi": bool(full.get("answer_multi"))}


# ---------- 对外 ----------
def status(username: str, course: str) -> dict:
    """摸底可用性 + 是否有没答完的。页面开局就要知道这两件事。"""
    c = get_course(course)
    pool = db.placement_pool(username, course)
    chapters = {p["chapter"] for p in pool}
    active = db.active_session(username, course, INTENT)

    payload = {
        "course": course, "course_display": c.display,
        "pool_size": len(pool), "min_items": MIN_LENGTH,
        "ready": len(pool) >= MIN_LENGTH,
        "chapters_available": len(chapters),
        "chapters_total": len(c.knowledge_taxonomy),
        "active": None,
    }
    if active:
        meta, items = _replay(active["id"])
        pending = next((i for i in items if not i["answered"]), None)
        payload["active"] = {
            "session_id": active["id"],
            "answered": sum(1 for i in items if i["answered"]),
            "total": int(meta.get("length") or DEFAULT_LENGTH),
            "started_at": active["started_at"],
            "item": _item_payload(pending["problem_id"], pending["seq"],
                                  int(meta.get("length") or DEFAULT_LENGTH),
                                  pending["chapter"]) if pending else None,
        }
    return payload


def start(username: str, course: str, length: int = DEFAULT_LENGTH,
          restart: bool = False) -> dict:
    """开一次摸底。已有未答完的会话时默认接着答，restart=True 才作废重开。"""
    active = db.active_session(username, course, INTENT)
    if active and not restart:
        meta, items = _replay(active["id"])
        pending = next((i for i in items if not i["answered"]), None)
        total = int(meta.get("length") or DEFAULT_LENGTH)
        if pending:
            return {"session_id": active["id"], "resumed": True, "total": total,
                    "answered": sum(1 for i in items if i["answered"]),
                    "item": _item_payload(pending["problem_id"], pending["seq"],
                                          total, pending["chapter"])}
        # 题都答完了却没收尾（上一次收尾时进程挂了之类），补一次
        return finalize(active["id"])
    if active:
        db.finish_session(active["id"], state="abandoned")

    pool = db.placement_pool(username, course)
    if len(pool) < MIN_LENGTH:
        raise ValueError(f"{get_course(course).short}的客观题只有 {len(pool)} 道，"
                         f"不足以做分级摸底（至少需要 {MIN_LENGTH} 道）")
    # 题池比要求的题数还少时按题池来：出不出得满是题库的事，不该让人卡在开始页
    total = max(MIN_LENGTH, min(MAX_LENGTH, int(length or DEFAULT_LENGTH)))
    total = min(total, len(pool))

    session_id = db.create_session(username, course_id=course, intent=INTENT, state="active")
    # 开始时把"这门课已经有掌握度记录的知识点"记进会话。收尾要靠它决定哪些点不覆盖，
    # 而那时库里已经被自己写过一遍了 —— 事后再查分不出"本来就有"和"刚写的"。
    # 也不能靠时间戳区分：库里只精确到秒，答得快的一整场摸底可能都落在同一秒。
    known = sorted({m["knowledge_id"] for m in db.list_mastery(username, course)})
    db.save_turn(session_id, "system", "入学摸底开始",
                 json.dumps({"length": total, "course": course, "known": known},
                            ensure_ascii=False))
    item = _pick(pool, set(), {}, START_DIFFICULTY)
    _ask(session_id, item, seq=1, target=START_DIFFICULTY)
    return {"session_id": session_id, "resumed": False, "total": total, "answered": 0,
            "item": _item_payload(item["problem_id"], 1, total, item["chapter"])}


def _ask(session_id: int, item: dict, seq: int, target: int) -> None:
    db.save_turn(session_id, "assistant", item["stem"], json.dumps({
        "seq": seq, "problem_id": item["problem_id"], "difficulty": item["difficulty"],
        "knowledge_id": item["knowledge_id"], "knowledge": item["knowledge"],
        "chapter": item["chapter"], "target": target}, ensure_ascii=False))


def _canonical_of(problem: dict):
    raw = problem.get("answer")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else None
    except (json.JSONDecodeError, TypeError):
        return None


def answer(username: str, session_id: int, student_answer: str, duration: int = 0) -> dict:
    """作答一题 → 判定 → 给下一题，或收尾出报告。"""
    sess = db.get_session(session_id)
    # 与 tutor.get_owned 同一条线：不是本人发起的会话，一律当作不存在。
    if not sess or sess["intent"] != INTENT or sess["username"] != (username or ""):
        raise ValueError("摸底会话不存在")
    if sess["completed_at"]:
        raise ValueError("这次摸底已经结束了")

    meta, items = _replay(session_id)
    total = int(meta.get("length") or DEFAULT_LENGTH)
    pending = next((i for i in items if not i["answered"]), None)
    if not pending:
        return finalize(session_id)

    full = db.get_problem(pending["problem_id"], with_solution=True) or {}
    canonical = _canonical_of(full)
    vr = check_numeric(student_answer, canonical)
    # passed 的三态在这里必须原样留住：None 是"作答没法解析"，不是答错。
    # 折成 False 会让一次输入失误既扣难度又进错题本。
    correct = vr.passed

    db.save_turn(session_id, "student", student_answer, json.dumps(
        {"seq": pending["seq"], "problem_id": pending["problem_id"],
         "correct": correct, "details": vr.details, "duration": max(0, int(duration or 0))},
        ensure_ascii=False))
    if correct is not None:
        # 判不了的那一题不记作答：score 只有 0/100 两个值，给它记 0 就是判错，
        # 之后会以"做错过"的身份混进正确率与选题冷却里。
        db.save_attempt(username, student_answer[:2000], 100.0 if correct else 0.0,
                        duration=max(0, int(duration or 0)),
                        problem_id=pending["problem_id"],
                        knowledge_id=pending["knowledge_id"], session_id=session_id)

    pending.update(answered=True, correct=correct)
    answered_n = sum(1 for i in items if i["answered"])
    if answered_n >= total:
        return {"correct": correct, "finished": True, "result": finalize(session_id)}

    pool = db.placement_pool(username, sess["course_id"])
    asked = {i["problem_id"] for i in items}
    counts: dict[str, int] = {}
    for i in items:
        counts[i["chapter"]] = counts.get(i["chapter"], 0) + 1
    target = _next_target(items)
    nxt = _pick(pool, asked, counts, target)
    if not nxt:
        # 题池被抽干了（题库比会话长度还小的边界）。提前收尾好过硬凑重复题。
        return {"correct": correct, "finished": True, "result": finalize(session_id)}
    _ask(session_id, nxt, seq=pending["seq"] + 1, target=target)
    return {"correct": correct, "finished": False,
            "answered": answered_n, "total": total,
            "item": _item_payload(nxt["problem_id"], pending["seq"] + 1, total, nxt["chapter"])}


def abandon(username: str, session_id: int) -> bool:
    sess = db.get_session(session_id)
    if not sess or sess["intent"] != INTENT or sess["username"] != (username or ""):
        raise ValueError("摸底会话不存在")
    return db.finish_session(session_id, state="abandoned")


def finalize(session_id: int) -> dict:
    """收尾：定级 → 写掌握度初值与错题 → 出报告。

    落库只发生一次，闸门是 `db.finish_session`（UPDATE ... WHERE completed_at IS NULL）。
    重复调用照样能拿到同一份报告，因为报告是从轮次重算出来的，不依赖那次写库。
    """
    # 先关闸再出报告：report 里的 finished 要读得到 completed_at，
    # 而"哪些点不覆盖"取自会话开始时记下的名单，不受这一步先后影响。
    first = db.finish_session(session_id)
    sess = db.get_session(session_id) or {}
    report = build_result(session_id)
    if first:
        _write_back(sess["username"], report)
    return report


def _write_back(username: str, report: dict) -> None:
    """把摸底结论落进掌握度与错题本。

    只写没有掌握度记录的知识点 —— 摸底是冷启动工具，不是重新评估工具。
    这个人要是已经练过这个点，那份估计基于真实作答，比一道摸底题可靠得多。
    """
    for point in report["points"]:
        if point["skipped"]:
            continue
        state = mastery_mod.derive_state(point["mastery"], point["asked"],
                                         recent_correct=point["correct_n"] > 0,
                                         has_misconception=False)
        db.upsert_mastery(username, point["knowledge_id"], point["mastery"],
                          mastery_mod.confidence(point["asked"]), state)
    for item in report["review"]:
        if item["correct"] is not False or not item["knowledge_id"]:
            continue
        db.save_error_record(username, error_type=item["error_type"],
                             diagnosis=f"入学摸底判错：{item['details']}",
                             knowledge=item["knowledge"], problem_stem=item["stem"],
                             problem_id=item["problem_id"], knowledge_id=item["knowledge_id"],
                             student_answer=str(item.get("answer") or ""))


def build_result(session_id: int) -> dict:
    """摸底报告。纯读、可重复调用 —— 报告永远是从轮次重算的，不缓存。"""
    sess = db.get_session(session_id) or {}
    course = sess.get("course_id") or "ds-algo"
    c = get_course(course)
    meta, items = _replay(session_id)
    answered = [i for i in items if i["answered"]]
    judged = [i for i in answered if i["correct"] is not None]

    review, points, chapters = [], {}, {}
    existing = set(meta.get("known") or [])     # 摸底开始那一刻就已有记录的点，见 start()
    for item in answered:
        full = db.get_problem(item["problem_id"], with_solution=True) or {}
        canon = _canonical_of(full)
        review.append({
            "seq": item["seq"], "problem_id": item["problem_id"],
            "stem": full.get("stem", ""), "difficulty": item["difficulty"],
            "knowledge_id": item["knowledge_id"], "knowledge": item["knowledge"],
            "chapter": item["chapter"], "your_answer": item["answer"],
            "answer": canonical_text(canon), "correct": item["correct"],
            "details": item["details"], "error_type": error_type_for_canonical(canon),
        })
    for item in judged:
        points.setdefault(item["knowledge_id"], []).append(item)
        chapters.setdefault(item["chapter"], []).append(item)

    point_rows = []
    for kid, group in points.items():
        level = level_of(group)
        # 已有掌握度记录的点：照样报出摸底测到的档位，但不写库，并说明原因。
        point_rows.append({
            "knowledge_id": kid, "name": group[0]["knowledge"], "chapter": group[0]["chapter"],
            "level": level, "mastery": mastery_for_level(level),
            "asked": len(group), "correct_n": sum(1 for i in group if i["correct"]),
            "skipped": kid in existing,
        })
    point_rows.sort(key=lambda r: (r["mastery"], r["name"]))

    chapter_rows = sorted(
        ({"chapter": ch or "未归章", "asked": len(group),
          "correct_n": sum(1 for i in group if i["correct"]),
          "level": level_of(group), "mastery": mastery_for_level(level_of(group))}
         for ch, group in chapters.items()),
        key=lambda r: r["mastery"])

    overall = level_of(judged)
    tested_points = len(points)
    return {
        "session_id": session_id, "course": course, "course_display": c.display,
        "finished": bool(sess.get("completed_at")),
        "finished_at": sess.get("completed_at") or "",
        "total": int(meta.get("length") or DEFAULT_LENGTH),
        "answered": len(answered), "judged": len(judged),
        "correct": sum(1 for i in judged if i["correct"]),
        "undetermined": len(answered) - len(judged),
        "duration_seconds": sum(i["duration"] for i in answered),
        "level": overall, "level_name": LEVEL_NAMES.get(overall or 0, ""),
        "level_hint": LEVEL_HINTS.get(overall or 0, ""),
        "level_mastery": mastery_for_level(overall) if overall is not None else None,
        "next_difficulty": profile_mod._target_difficulty(mastery_for_level(overall))
        if overall is not None else START_DIFFICULTY,
        "coverage": {
            "points_tested": tested_points,
            "points_total": len(c.topics),
            "points_written": sum(1 for r in point_rows if not r["skipped"]),
            "chapters_tested": len(chapters),
            "chapters_total": len(c.knowledge_taxonomy),
        },
        "points": point_rows,
        "chapters": chapter_rows,
        "review": review,
    }


def last_finished(username: str, course: str) -> dict | None:
    """最近一次做完的摸底（含报告）。没做过返回 None。

    结果页刷新之后还要看得到，所以走这条路重建，而不是让前端揣着上一次的返回值。
    """
    sess = db.last_completed_session(username, course, INTENT)
    return build_result(sess["id"]) if sess else None
