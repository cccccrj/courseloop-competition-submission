"""真题模考闭环：按 408 试卷结构组卷 → 限时作答 → 交卷阅卷 → 出分归因。

## 组卷是确定性的，阅卷不是

本模块只做组卷、计时和计分 —— 全是可复现的算术。**阅卷不在这里**：
它要走 `core_routes.judge_answer`（代码题跑沙箱、综合题可能回落 LLM 诊断），
那条路已经是全站唯一的判题口径，模考再写一套的话，同一份作答在练习页和
模考里会得出不同结论，而两处的结果都会写进同一个错题本、同一份掌握度。
分层跟 profile.py 一样：确定性的留在 learning/，要碰模型的交给上层编排。

## 分值怎么来的

`courses.exam_structure()`（可配置，默认 408）：单选 80 分（40 题 × 2）+ 综合应用题 70 分，共 150 分 / 180 分钟。
四门按 `Course.score`（45/45/35/25）用最大余额法分配：

    单选题数    12 / 12 / 9 / 7     = 40 题
    综合分值    21 / 21 / 16 / 12   = 70 分
    综合题数     2 /  2 / 2 /  1    = 7 题（21÷10 四舍五入，恰好是真卷的题数）

这些数字全部由课表里的分值算出来，不硬编码 —— 改分值、加课，试卷跟着变。
卷面总分同理走 `standard_score()`（Σ各门分值），408 四门下仍是 150。

**仍然写死的是 408 的卷面骨架**：单选 40 题、每题 2 分、时长 180 分钟
（CHOICE_COUNT / CHOICE_SCORE / DURATION_MINUTES）。加了第五门课时，
这 40 道单选会按分值重新分摊到五门上，而不是变成"另一张卷子" ——
要支持真正不同的卷面结构，这三个常量也得进 app_settings，那是后话。

## 题库不够就照实说

这是本模块最要紧的一条纪律。四门凑齐 47 道**能归到知识点**的题，对现在的题库
是个高要求（见 docs/18 第二档）。组不满时不降标准、不重复出题、也不悄悄缩短卷子：
按实际能出的题组卷，把每门每段缺多少写进 `shortfall`，满分标注为实际值并同时
给出标准值 150。一张 60 分满分的卷子考了 55 分是有意义的信息；把它印成"150 分制
考了 55"就是假的。

## 状态与摸底同构

`learning_sessions`（intent='exam'）+ `dialog_turns`：system 轮存整张卷子和分值，
student 轮存每次暂存的作答，assistant 轮存交卷后的逐题批改结果。
**批改结果必须存下来**，这一点与摸底不同 —— 摸底的报告可以随时从作答重算，
而模考的综合题批改可能调过模型，重算既慢又未必给出同一个结论。
"""
from __future__ import annotations

import json
from datetime import datetime

from backend.core import db
from backend.core.courses import all_courses, course_ids, exam_structure, get_course

INTENT = "exam"

# 试卷结构常量，对应 courses.exam_structure() 那句话
CHOICE_COUNT = 40           # 单选题数
CHOICE_SCORE = 2            # 每道单选分值
COMPREHENSIVE_TOTAL = 70    # 综合应用题总分
COMPREHENSIVE_UNIT = 10     # 综合题的名义分值，用来反推每门出几道
DURATION_MINUTES = 180

# 408 四门时的卷面总分，仅作为课表读不出来时的兜底。
DEFAULT_STANDARD_SCORE = CHOICE_COUNT * CHOICE_SCORE + COMPREHENSIVE_TOTAL   # 150


def standard_score() -> int:
    """一整卷的标准分 = 各门卷面分之和。

    原先这是写死的 `40×2+70=150`。它在 408 四门下恰好等于 45+45+35+25，
    所以两种算法一直看不出差别 —— 直到课程变成可配置：加一门 40 分的课，
    真实卷面变成 190（`blueprint()` 里每门的综合分是 `本门分值 − 本门单选分`，
    求和即 Σ分值），而常量仍报 150，`scaled_score` 会把 190 分的卷子按 150 折算。
    这不会报错，只会让所有人的分数系统性偏低。

    推导式对 408 四门给出的仍是 150，既有行为逐位不变。
    """
    from backend.core.courses import all_courses
    total = sum(c.score for c in all_courses().values())
    return total or DEFAULT_STANDARD_SCORE


# 低于这个题量就别开考了：连一门的单选都凑不齐，出来的不是模考是随堂小测
MIN_ITEMS = 8


def _apportion(total: int, weights: list[int]) -> list[int]:
    """按权重把 total 分成整数份（最大余额法），保证各份之和恰好等于 total。

    不用逐个四舍五入：那样 40 题按 45/45/35/25 分下去会得到 12+12+9+7=40 纯属运气，
    换一组分值就可能多一题或少一题，而试卷总分是不能浮动的。
    """
    if total <= 0 or not weights or sum(weights) <= 0:
        return [0] * len(weights)
    scale = sum(weights)
    exact = [total * w / scale for w in weights]
    base = [int(x) for x in exact]
    rest = total - sum(base)
    # 小数部分大的先补；完全相同时按原顺序（课程注册表顺序）定，结果可复现
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - base[i]), i))
    for i in order[:rest]:
        base[i] += 1
    return base


def blueprint() -> list[dict]:
    """试卷蓝图：每门出几道单选、几道综合、综合每道多少分。纯算术，不查库。"""
    courses = all_courses()
    ids = list(courses)
    weights = [courses[cid].score for cid in ids]
    choice_counts = _apportion(CHOICE_COUNT, weights)

    out = []
    for cid, choice_n in zip(ids, choice_counts):
        # 综合分 = 这门的卷面分 - 它的单选分，而不是把 70 分再按权重分一次。
        # 分两次分配的话，单选题数取整带来的零头没人认领：os 会变成 9×2+16=34 分、
        # net 变成 7×2+12=26 分，四门加起来仍是 150，但每门都不等于大纲写的
        # 45/45/35/25 了 —— 而按科目分值给权重正是这个项目的一条主线。
        # 相减则天然自洽：Σ卷面分 - Σ单选分 = 150 - 80 = 70，综合总分不用另算。
        comp_score = max(0, courses[cid].score - choice_n * CHOICE_SCORE)
        comp_n = max(1, round(comp_score / COMPREHENSIVE_UNIT))
        out.append({
            "course": cid, "short": courses[cid].short, "score": courses[cid].score,
            "choice_count": choice_n, "choice_score": CHOICE_SCORE,
            "comprehensive_score": comp_score, "comprehensive_count": comp_n,
            # 21 分两道题 → 11 + 10，而不是两个 10.5：分值是整数，考生看得懂的那种
            "comprehensive_item_scores": _apportion(comp_score, [1] * comp_n),
        })
    return out


def is_choice(problem: dict) -> bool:
    """单选题的判据是 canonical 的 kind，不是 problems.type 那个自由文本列。

    type 里存的是出题时随手写的中文（"单项选择"/"选择题"/"概念判断"…），
    按它分段迟早会漏。canonical 才是判分器真正吃的东西。

    公开（原 `_is_choice`）是因为 `scripts/grow_bank.py --kind choice` 要按同一把尺子
    数缺口。补题脚本自己再写一个判据的话，两边迟早会分叉，症状是"脚本说补够了、
    组卷仍报缺口"，而且极难查 —— 补的题在脚本眼里是选择题，在组卷眼里不是。
    """
    if problem.get("verification_type") != "numeric":
        return False
    try:
        canon = json.loads(problem.get("answer") or "")
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(canon, dict) and canon.get("kind") == "choice"


def is_past_paper(problem: dict) -> bool:
    """这道题是不是校对过的真题。判据是 source 前缀，与 ingest_past_papers 的写入口径一致。"""
    return str(problem.get("source") or "").startswith("past-paper")


def _select(pool: list[dict], count: int) -> list[dict]:
    """按章节轮转挑 count 道题，优先没做过的真题。确定性排序，同一题库同一份记录出同一张卷。

    轮转是为了让一门课的题分散在各章上 —— 全押在一章上的卷子测不出这门课的水平，
    也没法在成绩单上给出按章归因。

    排序里"真题优先"排在"没做过"之后：一道做过的真题测的是记忆，不如一道没做过的
    合成题有信息量；但同样没做过时，真题更接近考场的问法，先出真题。
    页面上写着"真题模考"，组卷就该真的先用真题。
    """
    if count <= 0:
        return []
    by_chapter: dict[str, list[dict]] = {}
    for item in sorted(pool, key=lambda p: (p["seen"], 0 if is_past_paper(p) else 1,
                                            p["problem_id"])):
        by_chapter.setdefault(item["chapter"], []).append(item)
    order = sorted(by_chapter)
    picked: list[dict] = []
    idx = 0
    while len(picked) < count and any(by_chapter[ch] for ch in order):
        bucket = by_chapter[order[idx % len(order)]]
        if bucket:
            picked.append(bucket.pop(0))
        idx += 1
    return picked


def build_paper(username: str) -> dict:
    """组一张卷。纯读，不落库 —— 预览和正式开考走同一份逻辑，看到的就是要考的。"""
    sections: list[dict] = []
    shortfall: list[dict] = []
    items: list[dict] = []

    for spec in blueprint():
        pool = db.bank_pool(username, spec["course"])
        choice_pool = [p for p in pool if is_choice(p)]
        # 综合题 = 剩下的一切：代码题、得分点题、非选择的数值题
        comp_pool = [p for p in pool if not is_choice(p)]

        chosen = _select(choice_pool, spec["choice_count"])
        comps = _select(comp_pool, spec["comprehensive_count"])
        for got, want, section in ((len(chosen), spec["choice_count"], "choice"),
                                   (len(comps), spec["comprehensive_count"], "comprehensive")):
            if got < want:
                shortfall.append({"course": spec["course"], "short": spec["short"],
                                  "section": section, "got": got, "want": want})
        sections.append({**spec, "choice_got": len(chosen), "comprehensive_got": len(comps)})
        for p in chosen:
            items.append({**_item_meta(p, spec["course"]), "section": "choice",
                          "score": CHOICE_SCORE})
        # 出不满时用前几道题的分值，剩下的分随缺口一起丢掉（不摊到别的题上：
        # 摊了就等于用 3 道题考出 70 分，每题权重被悄悄放大到不真实）
        for p, score in zip(comps, spec["comprehensive_item_scores"]):
            items.append({**_item_meta(p, spec["course"]), "section": "comprehensive",
                          "score": score})

    # 单选在前、综合在后，与真卷一致；同段内按课程注册表顺序
    order = {"choice": 0, "comprehensive": 1}
    items.sort(key=lambda it: (order[it["section"]], course_ids().index(it["course"])))
    for seq, item in enumerate(items, 1):
        item["seq"] = seq

    return {
        "items": items,
        "sections": sections,
        "shortfall": shortfall,
        "full_score": sum(it["score"] for it in items),
        # 本卷里有多少道是校对过的真题 —— 页面据此如实标注，不含糊其辞
        "past_paper_count": sum(1 for it in items if it.get("is_past_paper")),
        "standard_score": standard_score(),
        "duration_minutes": DURATION_MINUTES,
        "structure": exam_structure(),
        "ready": len(items) >= MIN_ITEMS,
        "min_items": MIN_ITEMS,
    }


def _item_meta(problem: dict, course: str) -> dict:
    return {"problem_id": problem["problem_id"], "course": course,
            "is_past_paper": is_past_paper(problem),
            "knowledge_id": problem["knowledge_id"], "knowledge": problem["knowledge"],
            "chapter": problem["chapter"], "difficulty": problem["difficulty"],
            "verification_type": problem["verification_type"]}


def item_payload(item: dict) -> dict:
    """发给考生的题面：题干 + 答题控件所需的形态，不含答案。"""
    full = db.get_problem(item["problem_id"]) or {}
    return {**item, "stem": full.get("stem", ""), "type": full.get("type", ""),
            "func_name": full.get("func_name", ""),
            "answer_kind": full.get("answer_kind", ""),
            "answer_unit": full.get("answer_unit", ""),
            "answer_multi": bool(full.get("answer_multi")),
            "test_count": full.get("test_count", 0)}


# ---------- 会话 ----------
def _loads(raw: str | None) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _replay(session_id: int) -> tuple[dict, dict[int, str], list[dict]]:
    """重放轮次 → (试卷元信息, {题号: 作答}, 逐题批改结果)。

    作答按轮次顺序覆盖：暂存是"最后写的算数"，不需要在库里更新旧行。

    批改结果同样按题号覆盖。阅卷是**边批边落**的（见 `save_progress`），库里因此是
    一串增量轮次、最后再补一条完整的 `graded`；续阅时重批某道题也只是重写同一个题号。
    按 seq 收进字典而不是往列表里追加：拼出来的这份东西要直接进 `score_paper` 求和，
    同一道题出现两次，那道题的分就算了两遍。
    """
    meta: dict = {}
    answers: dict[int, str] = {}
    graded: dict[int, dict] = {}
    for turn in db.list_turns(session_id):
        data = _loads(turn["structured_data"])
        if turn["role"] == "system":
            meta.update(data)
        elif turn["role"] == "student":
            for seq, text in (data.get("answers") or {}).items():
                answers[int(seq)] = text
        elif turn["role"] == "assistant":
            for row in (data.get("graded") or data.get("graded_delta") or []):
                if isinstance(row, dict) and row.get("seq") is not None:
                    graded[int(row["seq"])] = row
    return meta, answers, [graded[k] for k in sorted(graded)]


def start(username: str) -> dict:
    """开考。已有未交卷的场次直接接着考（时间照旧从当初开考算起）。"""
    active = _active_any(username)
    if active:
        return session_state(username, active["id"])

    paper = build_paper(username)
    if not paper["ready"]:
        raise ValueError(f"题库里能组卷的题只有 {len(paper['items'])} 道"
                         f"（至少需要 {MIN_ITEMS} 道），先补题库再开考")
    # course_id 存空串：这是四门合卷，挂在任何一门名下都是错的
    session_id = db.create_session(username, course_id="", intent=INTENT, state="active")
    db.save_turn(session_id, "system", "模考开始", json.dumps({
        "items": paper["items"], "sections": paper["sections"], "shortfall": paper["shortfall"],
        "full_score": paper["full_score"], "standard_score": paper["standard_score"],
        "duration_minutes": paper["duration_minutes"]}, ensure_ascii=False))
    return session_state(username, session_id)


def _active_any(username: str) -> dict | None:
    return db.active_session(username, "", INTENT)


def owned(username: str, session_id: int) -> dict:
    sess = db.get_session(session_id)
    # 与 tutor.get_owned 同一条线：不是本人的场次一律当作不存在
    if not sess or sess["intent"] != INTENT or sess["username"] != (username or ""):
        raise ValueError("模考场次不存在")
    return sess


def _remaining(sess: dict, meta: dict) -> int:
    """剩余秒数。由服务端的开考时间算，不信客户端的计时器 —— 页面一刷新它就归零了。"""
    limit = int(meta.get("duration_minutes") or DURATION_MINUTES) * 60
    try:
        started = datetime.strptime(sess["started_at"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, KeyError):
        return limit
    return max(0, limit - int((datetime.now() - started).total_seconds()))


def session_state(username: str, session_id: int) -> dict:
    """考试中的一次性快照：卷子 + 已暂存的作答 + 剩余时间。"""
    sess = owned(username, session_id)
    meta, answers, graded = _replay(session_id)
    items = meta.get("items") or []
    return {
        "session_id": session_id,
        "started_at": sess["started_at"],
        "submitted": bool(sess["completed_at"]),
        "duration_minutes": meta.get("duration_minutes", DURATION_MINUTES),
        "remaining_seconds": 0 if sess["completed_at"] else _remaining(sess, meta),
        "full_score": meta.get("full_score", 0),
        "standard_score": meta.get("standard_score", standard_score()),
        "sections": meta.get("sections") or [],
        "shortfall": meta.get("shortfall") or [],
        "items": [item_payload(it) for it in items],
        "answers": {str(k): v for k, v in answers.items()},
        "graded": bool(graded),
    }


def save_answers(username: str, session_id: int, answers: dict) -> int:
    """暂存作答。返回本次写下的条数。"""
    sess = owned(username, session_id)
    if sess["completed_at"]:
        raise ValueError("这场模考已经交卷了")
    clean = {str(int(seq)): str(text)[:4000] for seq, text in (answers or {}).items()}
    if clean:
        db.save_turn(session_id, "student", f"暂存 {len(clean)} 题",
                     json.dumps({"answers": clean}, ensure_ascii=False))
    return len(clean)


def pending(username: str) -> dict | None:
    """这个人有没有没考完的场次。首页/模考页开局要知道。"""
    sess = _active_any(username)
    return session_state(username, sess["id"]) if sess else None


def grading_input(username: str, session_id: int) -> tuple[list[dict], dict[int, str], dict]:
    """阅卷要的三样：题目、作答、试卷元信息。判题本身在上层做（见模块 docstring）。"""
    owned(username, session_id)
    meta, answers, _ = _replay(session_id)
    return (meta.get("items") or []), answers, meta


def graded_rows(session_id: int) -> list[dict]:
    """这场已经批出来的题。续阅卷时用它跳过批过的，不重判、不重复写错题本。"""
    return _replay(session_id)[2]


def stored_report(session_id: int) -> dict | None:
    """存下来的成绩单；没阅完就没有。

    它同时是"这场阅完了没有"的判据：`completed_at` 只说明交了卷，
    而交卷与阅完之间隔着几十次判题，正是这中间断掉最麻烦。
    """
    for turn in db.list_turns(session_id):
        data = _loads(turn["structured_data"])
        if data.get("report"):
            return data["report"]
    return None


def finish(session_id: int) -> bool:
    """关闸：交卷只能发生一次。

    抢到的那次负责阅卷。**抢不到不等于没事可做** —— 阅卷可能在上一次里批到一半
    就断了（关页面、断网、后端重启），那场的 `completed_at` 已经写上，成绩单却还
    不存在。所以调用方拿到 False 之后要去看 `stored_report`：有成绩单就直接给，
    没有就接着批剩下的题。早先这里返回 False 就抛 409，那场模考从此没有出口 ——
    题批了一半、错题本记了一半，成绩单永远是 0 分。
    """
    return db.finish_session(session_id, state="completed")


def save_progress(session_id: int, rows: list[dict]) -> None:
    """把刚批完的几道题落库。

    整场阅卷可能要几分钟（综合题一道一次模型），全批完再一次性写，中间任何一次
    中断就等于这几分钟白费、而且是**不可重来的白费**：交卷闸已经落下了。
    每道题批完就落一条增量，续阅时从这里接着算。
    """
    if not rows:
        return
    db.save_turn(session_id, "assistant", f"批完 {len(rows)} 题",
                 json.dumps({"graded_delta": rows}, ensure_ascii=False))


def save_graded(session_id: int, graded: list[dict], report: dict) -> None:
    """把逐题批改与成绩单存进会话。

    与摸底不同，这里存的是**结果**而不是靠重算：综合题批改可能调过模型，
    重算既慢又未必给出同一个结论，而成绩单是要能反复打开的。
    """
    db.save_turn(session_id, "assistant", "阅卷完成",
                 json.dumps({"graded": graded, "report": report}, ensure_ascii=False))


def unfinished_grading(username: str, limit: int = 5) -> dict | None:
    """最近一场"交了卷但没阅完"的模考，没有则 None。

    模考页开局要问这个：考生看到的是自己刚交完卷、页面却回到了开考前 —— 那一场
    在他眼里已经消失了。给出 session_id，前端再发一次交卷就能接着阅。
    """
    for row in db.list_sessions(username, course_id="", intent=INTENT, limit=limit):
        if not row.get("completed_at") or row.get("state") != "completed":
            continue
        if stored_report(row["id"]) is not None:
            return None                      # 最近一场已经阅完，没有要续的
        meta, _, graded = _replay(row["id"])
        return {"session_id": row["id"], "submitted_at": row["completed_at"],
                "graded": len(graded), "total": len(meta.get("items") or [])}
    return None


def history(username: str, limit: int = 10) -> list[dict]:
    """历次模考：什么时候考的、多少分、阅完没有。

    `/exam/result/{id}` 从上线起就能反复打开一份成绩单，但没有任何地方列出
    "我考过哪几场" —— 拿不到 id 的入口，等于这些成绩单不存在。
    """
    out = []
    for row in db.list_sessions(username, course_id="", intent=INTENT, limit=limit):
        if not row.get("completed_at"):
            continue                          # 还在考的那场不算历史
        report = stored_report(row["id"])
        out.append({
            "session_id": row["id"], "started_at": row["started_at"],
            "submitted_at": row["completed_at"], "state": row["state"],
            "graded": report is not None,
            "score": report.get("score") if report else None,
            "full_score": report.get("full_score") if report else None,
            "scaled_score": report.get("scaled_score") if report else None,
            "standard_score": report.get("standard_score") if report else standard_score(),
        })
    return out


def score_paper(items: list[dict], graded: list[dict], meta: dict) -> dict:
    """把逐题得分汇成成绩单。纯算术。"""
    by_seq = {g["seq"]: g for g in graded}
    total = sum(g["score"] for g in graded)
    full = sum(it["score"] for it in items)

    sections: dict[str, dict] = {}
    courses: dict[str, dict] = {}
    chapters: dict[tuple, dict] = {}
    for it in items:
        g = by_seq.get(it["seq"]) or {"score": 0.0, "passed": False}
        for bucket, key, extra in ((sections, it["section"], {}),
                                   (courses, it["course"], {"short": get_course(it["course"]).short}),
                                   (chapters, (it["course"], it["chapter"]),
                                    {"course": it["course"], "chapter": it["chapter"] or "未归章"})):
            row = bucket.setdefault(key, {"score": 0.0, "full": 0.0, "count": 0, "correct": 0, **extra})
            row["score"] += g["score"]
            row["full"] += it["score"]
            row["count"] += 1
            row["correct"] += 1 if g.get("passed") else 0

    def _rate(row: dict) -> dict:
        return {**row, "score": round(row["score"], 1), "full": round(row["full"], 1),
                "rate": round(row["score"] / row["full"], 4) if row["full"] else 0.0}

    weak = sorted((_rate(v) for v in chapters.values()), key=lambda r: r["rate"])
    return {
        "score": round(total, 1),
        "full_score": round(full, 1),
        "standard_score": meta.get("standard_score", standard_score()),
        "rate": round(total / full, 4) if full else 0.0,
        # 折算到标准分制（408 下就是 150）。题库不满时这个数才是可比的，但它是
        # **折算**，不是考出来的，前端必须标明 —— 60 分卷考 55 折成 137.5，
        # 不说清楚就是在虚报。标准分取自课表求和，改了课就跟着改。
        "scaled_score": round(total / full * meta.get("standard_score", standard_score()), 1) if full else 0.0,
        "sections": {k: _rate(v) for k, v in sections.items()},
        "courses": [_rate(v) | {"course": k} for k, v in courses.items()],
        "weak_chapters": weak[:8],
        "answered": sum(1 for g in graded if (g.get("answer") or "").strip()),
        "correct": sum(1 for g in graded if g.get("passed")),
        "count": len(items),
    }


def build_report(username: str, session_id: int) -> dict:
    """成绩单。交卷后可反复打开，读的是存下来的批改结果。"""
    sess = owned(username, session_id)
    meta, answers, graded = _replay(session_id)
    items = meta.get("items") or []
    stored = None
    for turn in db.list_turns(session_id):
        data = _loads(turn["structured_data"])
        if data.get("report"):
            stored = data["report"]
    report = stored or score_paper(items, graded, meta)
    return {
        "session_id": session_id, "submitted": bool(sess["completed_at"]),
        "submitted_at": sess["completed_at"] or "",
        "started_at": sess["started_at"],
        "duration_minutes": meta.get("duration_minutes", DURATION_MINUTES),
        "shortfall": meta.get("shortfall") or [],
        "report": report,
        "items": [{**item_payload(it),
                   **{k: v for k, v in (next((g for g in graded if g["seq"] == it["seq"]), {})).items()
                      if k != "seq"},
                   "your_answer": answers.get(it["seq"], "")}
                  for it in items],
    }
