"""班级视角的聚合：掌握度热力图、共性错因、作业下发与验收。

## 这一层为什么存在

助教工作台原来只有四个总计数（学生数 / 活跃数 / 作答数 / 错题数）和一张全局错因排行。
它能回答"班里在发生什么"，回答不了**"谁在哪一章卡住了"** —— 而后者才是助教要做的事。
本模块把 db 出的原始聚合拼成两个可以直接看的东西：

- **热力图**：学生 × 知识点的掌握度矩阵。行是人，列是知识点，一眼扫出
  "整列都暗" = 这个点全班都没过，"整行都暗" = 这个学生掉队了。
- **作业**：下发一组知识点 + 目标题数，验收完全由 `practice_attempts` 算出来
  （见 `db.assignment_done_matrix` —— 不另存完成状态）。

分层与 profile.py 一致：db.py 出原始行（纯 SQL），这里做拼装与派生，不碰 LLM。

## 只画有数据的格子

热力图不给"未评估"补零。掌握度 0 和"没测过"在颜色上长得一模一样，而它们对助教
意味着完全相反的两件事（一个是教了没学会，一个是还没教到）。所以未评估的格子
不给值，前端画成留白，`covered` 字段如实报覆盖率。
"""
from __future__ import annotations

from datetime import date

from backend.core import db
from backend.core.courses import get_course
from backend.learning import profile as profile_mod
from backend.learning.error_taxonomy import follow_up_of, name_of

# 热力图的规模上限。再大就不是"一眼看清"而是一张需要横向滚动很久的表；
# 真到那个量级，该做的是先按班筛选，而不是把整张表铺开。
MAX_STUDENTS = 60
MAX_POINTS = 40
# 作业效果：下发后参与作答的学生少于这个数，只显示数字、不给"有效/无效"的判断
MIN_EFFECT_SAMPLE = 5
# 「今日」首屏的风险线：平均掌握度低于它、或超过这么多天没作答
RISK_MASTERY = 0.45
IDLE_DAYS = 7


def _student_mastery(course: str, class_name: str = "") -> dict[str, dict]:
    """每个学生：练过的点 → {mastery, state}（带遗忘衰减），外加平均掌握度摘要。

    走 profile.build_profile，与学生自己在首页 / 记录页看到的是同一口径（I-03）。
    此前这里直接读 mastery_records 原始值，平均也只对热力图上显示的那几列求，
    于是助教看到的"平均掌握度"和学生自己看到的是三个不同的数。
    """
    out: dict[str, dict] = {}
    for stu in db.list_students(class_name):
        points = profile_mod.build_profile(stu["username"], course)
        out[stu["username"]] = {
            "cells": {p["knowledge_id"]: {"mastery": round(float(p["mastery"]), 4), "state": p["state"]}
                      for p in points if p.get("practiced")},
            "summary": profile_mod.summarize_mastery(points),
        }
    return out


def heatmap(course: str, class_name: str = "") -> dict:
    """班级 × 知识点掌握度热力图。纯读。"""
    c = get_course(course)
    points = db.class_points(course)
    # 列的取舍：按"最该被看见"排 —— 未订正错题多的、平均掌握度低的排前面。
    # 单纯按大纲顺序排的话，前几列往往是绪论这种全班都会的章节，占满了屏宽。
    points.sort(key=lambda p: (-(p["error_open"] or 0), p["avg_mastery"] or 0.0,
                               p["knowledge_id"]))
    points = points[:MAX_POINTS]
    point_ids = [p["knowledge_id"] for p in points]

    per_student = _student_mastery(course, class_name)

    activity = {a["username"]: a for a in db.student_activity(course, class_name)}
    students = []
    for stu in db.list_students(class_name):
        mine = per_student.get(stu["username"], {"cells": {}, "summary": {}})
        act = activity.get(stu["username"], {})
        summary = mine["summary"]
        students.append({
            **stu,
            # 格子只画屏幕上的这几列；平均和覆盖数按**全部**练过的点算，
            # 否则同一个学生在这里和在他自己首页上看到的平均值不一样
            "cells": {str(k): v for k, v in mine["cells"].items() if k in point_ids},
            "covered": summary.get("practiced", 0),
            # 平均只对**练过的**点求：拿未评估当 0 会让刚入学的学生和学了但没学会的
            # 学生并列垫底，而这两种情况的处理方式完全不同。
            "avg_mastery": summary.get("average"),
            "attempts": act.get("attempts", 0),
            "correct_n": act.get("correct_n", 0),
            "last_at": act.get("last_at") or "",
        })
    # 掉队的排前面：没数据的（avg 为 None）单独垫底，他们的问题是"还没开始"，
    # 与"学了但弱"不是一类，混在一起排会把真正需要盯的人挤下去。
    students.sort(key=lambda s: (s["avg_mastery"] is None, s["avg_mastery"] or 0.0,
                                 s["username"]))
    students = students[:MAX_STUDENTS]

    return {
        "course": course, "course_display": c.display, "class_name": class_name,
        "classes": db.list_classes(),
        "points": [{**p, "avg_mastery": round(float(p["avg_mastery"] or 0.0), 4)}
                   for p in points],
        "students": students,
        "point_total": len(c.topics),
        "point_shown": len(points),
        "student_total": len(db.list_students(class_name)),
    }


# ---------- 导出 ----------
def export_rows(kind: str, course: str, class_name: str = "",
                assignment_id: int = 0) -> tuple[list[str], list[list]]:
    """导出用的 (表头, 行)。三种口径，都与页面上看到的**同一份数据**。

    为什么要导出：老师看完热力图想把名单交上去、或者做线下分析，此前没有任何出口，
    只能截图 —— 截出来的数据我们也留不下。而"这个班期中掌握度"这类东西，
    老师是要贴进他自己的表格里的。

    刻意不做的：不导密码材料、不导头像、不导别的班。导出是最容易顺手多带一列
    的地方，而多带的那一列一旦进了老师的表格，就再也收不回来。

    **导出不做截断。** 热力图为了能看，硬截了 60 人 / 40 个知识点；导出没有这个
    理由 —— 老师导出来正是为了看全。所以这里直接走 db 层，不复用 heatmap()。
    """
    if kind == "mastery":
        points = sorted(db.class_points(course),
                        key=lambda p: (-(p["error_open"] or 0), p["avg_mastery"] or 0.0,
                                       p["knowledge_id"]))
        names = [p["name"] for p in points]
        ids = [p["knowledge_id"] for p in points]
        per_student = _student_mastery(course, class_name)
        activity = {a["username"]: a for a in db.student_activity(course, class_name)}
        header = ["学号", "姓名", "班级", "作答数", "正确数", "最后作答",
                  "平均掌握度（已练知识点）", "已练知识点数", *names]
        rows = []
        for stu in db.list_students(class_name):
            mine = {k: v["mastery"] for k, v in
                    per_student.get(stu["username"], {"cells": {}})["cells"].items()}
            summary = per_student.get(stu["username"], {}).get("summary") or {}
            act = activity.get(stu["username"], {})
            avg = summary.get("average")
            rows.append([stu.get("student_id", ""), stu.get("name", ""),
                         stu.get("class_name", ""), act.get("attempts", 0),
                         act.get("correct_n", 0), act.get("last_at", ""),
                         round(avg, 4) if avg is not None else "", summary.get("practiced", 0),
                         # 未评估留空，不写 0 —— 与页面上留白同一个理由：
                         # 0 分和没测过是两回事，写成 0 会让人在 Excel 里算出个假平均
                         *[round(mine[k], 4) if k in mine else "" for k in ids]])
        return header, rows

    if kind == "assignment":
        a = db.get_assignment(assignment_id)
        if not a:
            raise ValueError("这份作业不存在或已被删除")
        view = assignment_view(a)
        header = ["学号", "姓名", "班级", "达标知识点", "知识点总数", "做对题数", "是否完成"]
        return header, [[s.get("student_id", ""), s.get("name", ""), s.get("class_name", ""),
                         s["points_done"], s["points_total"], s["done"],
                         "是" if s["completed"] else "否"] for s in view["students"]]

    if kind == "errors":
        header = ["学号", "姓名", "班级", "知识点", "错因", "诊断", "是否订正",
                  "助教复核", "助教备注", "时间"]
        rows = []
        for stu in db.list_students(class_name):
            for e in db.list_error_records(stu["username"], limit=200, course=course):
                rows.append([stu.get("student_id", ""), stu.get("name", ""),
                             stu.get("class_name", ""), e.get("knowledge") or "",
                             name_of(e.get("error_type", "")),
                             (e.get("diagnosis") or "").split(" || ")[-1],
                             "是" if e.get("resolved") else "否",
                             "已复核" if e.get("ta_reviewed") else "",
                             e.get("ta_note") or "", e.get("created_at") or ""])
        return header, rows

    raise ValueError("不支持这种导出")


def common_errors(course: str = "", limit: int = 12, class_name: str = "") -> list[dict]:
    return [{"error_type": r["error_type"], "error_name": name_of(r["error_type"]),
             "count": r["cnt"]}
            for r in db.aggregate_error_types(limit, course_id=course, class_name=class_name)]


# ---------- 作业 ----------
def _point_names(course: str, ids: list[int]) -> dict[int, str]:
    return {p["id"]: p["name"] for p in db.list_knowledge_points(course) if p["id"] in set(ids)}


def _progress(per_point: dict[int, int], knowledge_ids: list[int], target: int) -> dict:
    """把"每个点做对几道"折成一份完成度。

    `target` 是**每个知识点**的目标题数（下发表单、作业列表、学生首页三处文案
    都这么写）。所以：每个点都达标才算完成，进度条按**逐点封顶**后再求和 ——
    不封顶的话，在一个点上多做几道就能把进度条推满，而另外两个点一道没碰。
    """
    done_by = {kid: per_point.get(kid, 0) for kid in knowledge_ids}
    reached = [kid for kid, n in done_by.items() if n >= target]
    capped = sum(min(n, target) for n in done_by.values())
    total = target * max(1, len(knowledge_ids))
    return {"done": sum(done_by.values()),          # 总做对题数，给人看"做了多少"
            "done_by_point": done_by,
            "points_done": len(reached), "points_total": len(knowledge_ids),
            "target": target,
            "completed": len(reached) == len(knowledge_ids) and bool(knowledge_ids),
            "rate": round(min(1.0, capped / total), 4)}


def assignment_view(assignment: dict, *, with_students: bool = True) -> dict:
    """一条作业 + 验收情况。

    完成度按"下发之后**在每一个**指定知识点上做对了几道不同的题"算，
    每个点都达到 target_count 才记完成（口径见 `_progress`）。
    """
    names = _point_names(assignment["course_id"], assignment["knowledge_ids"])
    matrix = db.assignment_done_matrix(assignment["knowledge_ids"], assignment["created_at"])
    target = max(1, int(assignment["target_count"] or 1))

    # 来源在库里是个错因 code，界面要的是人话。翻译放这里而不是前端：
    # 助教端和学生端都要显示它，两处各译一份迟早对不上。
    src = (assignment.get("source_error_type") or "").strip()
    out = {**assignment,
           "points": [{"knowledge_id": kid, "name": names.get(kid, f"知识点{kid}")}
                      for kid in assignment["knowledge_ids"]],
           "source_name": name_of(src) if src else "",
           "source_follow_up": follow_up_of(src) if src else "",
           "overdue": bool(assignment["due_date"]) and assignment["due_date"] < date.today().isoformat()
           and assignment["status"] == "open"}
    if not with_students:
        return out

    rows = []
    for stu in db.list_students(assignment["class_name"] or ""):
        rows.append({**stu, **_progress(matrix.get(stu["username"], {}),
                                        assignment["knowledge_ids"], target)})
    rows.sort(key=lambda r: (r["completed"], r["points_done"], r["done"], r["username"]))
    out["students"] = rows
    out["completed_students"] = sum(1 for r in rows if r["completed"])
    out["total_students"] = len(rows)
    out["completion_rate"] = round(out["completed_students"] / len(rows), 4) if rows else 0.0
    # 效果验证：只报数，样本不够不下结论（docs/35 P1-7）
    effect = db.assignment_effect(assignment["knowledge_ids"], assignment["created_at"],
                                  [r["username"] for r in rows])
    effect["enough"] = effect["students_active"] >= MIN_EFFECT_SAMPLE
    out["effect"] = effect
    return out


def student_assignments(username: str, course: str) -> list[dict]:
    """学生这边看到的作业：面向全体的 + 面向本班的，只列未关闭的。"""
    profile = db.get_user_profile(username) or {}
    rows = db.list_assignments(course, open_only=True,
                               class_name=profile.get("class_name") or "")
    out = []
    for row in rows:
        names = _point_names(row["course_id"], row["knowledge_ids"])
        per_point = db.assignment_done_matrix(row["knowledge_ids"], row["created_at"],
                                              username=username).get(username, {})
        target = max(1, int(row["target_count"] or 1))
        prog = _progress(per_point, row["knowledge_ids"], target)
        out.append({
            "id": row["id"], "title": row["title"], "note": row["note"],
            "course": row["course_id"], "due_date": row["due_date"] or "",
            # 学生也该知道这份作业为什么落到自己头上。给名称不给 code：
            # 「班级里这个错因出现得多」是他能理解的理由，错因 code 不是。
            "source_name": name_of(row["source_error_type"]) if (row.get("source_error_type") or "") else "",
            # 逐点的完成数也给出去：学生要知道还差哪个点，不然"还差 2 题"
            # 他会挑最顺手的那个点去做，而那个点多半已经达标了
            "points": [{"knowledge_id": kid, "name": names.get(kid, f"知识点{kid}"),
                        "done": prog["done_by_point"].get(kid, 0)}
                       for kid in row["knowledge_ids"]],
            **{k: v for k, v in prog.items() if k != "done_by_point"},
            "overdue": bool(row["due_date"]) and row["due_date"] < date.today().isoformat(),
        })
    # 没做完的排前面，其中临近截止的更前
    out.sort(key=lambda a: (a["completed"], a["due_date"] or "9999-12-31", a["id"]))
    return out


# ---------- 助教「今日」首屏（docs/35 P1-6） ----------
def today(course: str, class_name: str = "") -> dict:
    """助教今天先做什么：一个主行动 + 三张摘要（待复核 / 作业 / 风险学生）。

    主行动按"有人在等 → 已经晚了 → 还没处理的共性问题 → 掉队的人"排：
      1. 学生主动申请的复核（错题标记 + 大厅问答申请）—— 有人在等回复；
      2. 已逾期、还有人没完成的作业 —— 晚了，要么催，要么调整；
      3. 本门最常见、却还没有任何作业针对的错因 —— 该下发干预了；
      4. 平均掌握度最低 / 很久没作答的学生 —— 点开看看；
      5. AI 结论存疑的错题（验证未通过 / 无依据 / 粗心）—— 复核一下。
    都没有就明说"今天没有急事"，不硬造一个按钮。
    """
    reasons = db.unreviewed_by_reason(course, class_name)
    _, qa_open = db.list_review_requests(course_id=course, status="open", limit=1)
    student_waiting = reasons.get("student_request", 0) + qa_open
    doubtful = sum(reasons.get(k, 0) for k in ("verify_failed", "no_evidence", "careless"))

    views = [assignment_view(a) for a in db.list_assignments(course, open_only=True)]
    if class_name:
        views = [v for v in views if v["class_name"] in ("", class_name)]
    overdue = [v for v in views if v["overdue"] and v["completed_students"] < v["total_students"]]
    covered = {v.get("source_error_type") for v in views if v.get("source_error_type")}
    commons = common_errors(course, class_name=class_name)
    uncovered = next((c for c in commons if c["error_type"] not in covered and c["count"] >= 3), None)

    hm = heatmap(course, class_name)
    today_s = date.today().isoformat()
    risk = []
    for stu in hm["students"]:
        idle = bool(stu["last_at"]) and (date.fromisoformat(stu["last_at"][:10]) <
                                          date.fromordinal(date.today().toordinal() - IDLE_DAYS))
        low = stu["avg_mastery"] is not None and stu["avg_mastery"] < RISK_MASTERY
        if low or idle:
            risk.append({"username": stu["username"], "name": stu.get("name") or stu["username"],
                         "avg_mastery": stu["avg_mastery"], "last_at": stu["last_at"],
                         "why": "平均掌握度偏低" if low else f"超过 {IDLE_DAYS} 天没有作答"})
    risk.sort(key=lambda r: (r["avg_mastery"] is None, r["avg_mastery"] or 0))

    if student_waiting:
        primary = {"kind": "review", "tab": "queue", "reason": "student_request",
                   "title": f"有 {student_waiting} 条学生主动申请的复核在等你",
                   "why": "学生觉得 AI 判得不对或回答依据不足，点了「请助教看看」。",
                   "button": "去处理复核申请"}
    elif overdue:
        a = min(overdue, key=lambda v: v["completion_rate"])
        left = a["total_students"] - a["completed_students"]
        primary = {"kind": "assignment", "tab": "assignment", "assignment_id": a["id"],
                   "title": f"「{a['title']}」已逾期，还有 {left} 人没完成",
                   "why": f"截止 {a['due_date']}，完成率 {round(a['completion_rate'] * 100)}%。",
                   "button": "查看名单"}
    elif uncovered:
        primary = {"kind": "intervene", "tab": "assignment", "source": uncovered["error_type"],
                   "title": f"「{uncovered['error_name']}」是本门最常见的错因（{uncovered['count']} 条），还没有作业针对它",
                   "why": f"建议处置：{follow_up_of(uncovered['error_type'])}。",
                   "button": "针对它下发作业"}
    elif risk:
        r = risk[0]
        primary = {"kind": "student", "tab": "", "username": r["username"],
                   "title": f"{r['name']}：{r['why']}",
                   "why": (f"平均掌握度 {round(r['avg_mastery'] * 100)}%。" if r["avg_mastery"] is not None
                           else "还没有掌握度记录。"),
                   "button": "看看这个学生"}
    elif doubtful:
        primary = {"kind": "review", "tab": "queue", "reason": "",
                   "title": f"有 {doubtful} 条 AI 结论存疑的错题待复核",
                   "why": "验证未通过、没检索到教材依据，或判成了粗心。",
                   "button": "去复核"}
    else:
        primary = {"kind": "none", "title": "今天没有急事",
                   "why": "没有学生在等复核，作业都没逾期，共性错因都已有作业针对。", "button": ""}

    return {
        "course": course, "class_name": class_name, "date": today_s,
        "primary": primary,
        "review": {"student_waiting": student_waiting, "qa_open": qa_open,
                   "by_reason": reasons, "doubtful": doubtful,
                   "total": sum(reasons.values()) + qa_open},
        "assignments": {"open": len(views), "overdue": len(overdue),
                        "items": [{"id": v["id"], "title": v["title"], "overdue": v["overdue"],
                                   "completion_rate": v["completion_rate"],
                                   "completed_students": v["completed_students"],
                                   "total_students": v["total_students"],
                                   "effect": v.get("effect")} for v in views[:5]]},
        "risk_students": risk[:5],
        "uncovered_error": uncovered,
    }
