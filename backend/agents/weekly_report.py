"""学情周报 agent：把一周的学习记录汇成一页可读、可分享的报告。

## 分工

**数字全部由确定性聚合出**（db.window_activity / window_errors / mastery_as_of +
profile.build_profile），LLM 只负责把这些数字写成一段话和几条建议。反过来做——
让模型自己去"总结学习情况"——它会顺手编出没发生过的进步，而周报是最容易被
截图转发出去的一页，编错的代价比任何内页都大。

## 没有数据时说没有数据

三种"空"必须分开讲，因为对学生意味着不同的下一步：

- **这周没练**（attempts=0）：不生成点评，直接说这周没有记录。凑一段"继续加油"
  会让人以为系统在跟踪，其实什么都没跟踪到。
- **没有上周基线**（mastery_snapshots 里查不到窗口起点之前的快照）：掌握度变化
  一律返回 None 而不是 0.0。0.0 读作"这周没涨"，None 读作"还没法比"。
- **LLM 未配置**：数字照出，点评留空。周报的主体是数字，不该因为模型不在就整页打不开。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from backend.core import db
from backend.core import llm as llm_mod
from backend.core import telemetry
from backend.core.config import settings
from backend.core.courses import get_course
from backend.learning import profile as profile_mod
from backend.learning.error_taxonomy import name_of

# 报告里点名的知识点条数。多了就不是"周报"是"清单"了，一页放不下也没人看。
_TOP_N = 5

REPORT_SYSTEM = (
    "你是一名 408 考研辅导老师，正在给学生写本周的学习小结。\n"
    "写作要求：\n"
    "1) 只依据给出的数据说话。数据里没有的（比如学习态度、做题速度快慢）一个字都不要提；\n"
    "2) 先用两三句话点评本周实际发生了什么，再给 2-3 条下周的具体建议，"
    "建议必须指向给出的知识点名，不要泛泛说「多做题」；\n"
    "3) 数字直接引用给出的值，不要自己重新算，也不要四舍五入成别的数；\n"
    "4) 语气平实，像老师当面说话，不要用「亲爱的同学」这类客套开场，不要用感叹号堆情绪；\n"
    "5) 纯文本，不超过 220 字，不要 Markdown 标题、不要列表符号，"
    "也不要用 ** 或 __ 加粗、* 或 _ 斜体 —— 前端这一段按纯文本渲染，"
    "写了标记只会原样显示成星号和下划线。"
)


def week_bounds(offset: int = 0, today: date | None = None) -> tuple[str, str]:
    """第 offset 周（0=本周）的 [周一, 下周一)，返回两个 'YYYY-MM-DD'。

    右开区间：窗口内的比较一律用 `< end`，避免"上周日 23:59 的作答算进哪一周"
    这类边界要各处各自约定。周一起算是国内课表的习惯。
    """
    anchor = today or date.today()
    monday = anchor - timedelta(days=anchor.weekday()) + timedelta(weeks=-abs(offset))
    return monday.isoformat(), (monday + timedelta(days=7)).isoformat()


def _mastery_delta(username: str, course: str, start: str,
                   points: list[dict]) -> tuple[float | None, list[dict]]:
    """(全门平均掌握度变化, 涨得最多的知识点)。没有基线快照时返回 (None, [])。

    基线取窗口起点**零点之前**的最后一条快照。比较只在两边都有值的知识点上做 ——
    拿本周新练的点跟"基线 0"比，涨幅会被这些从无到有的点撑得很好看，
    那不是这周的进步，是这个点第一次有数据。
    """
    baseline = db.mastery_as_of(username, course, f"{start} 00:00:00")
    if not baseline:
        return None, []
    deltas = []
    for p in points:
        before = baseline.get(p["knowledge_id"])
        if before is None:
            continue
        deltas.append({**p, "before": round(before, 4),
                       "delta": round(float(p["mastery_raw"]) - before, 4)})
    if not deltas:
        return None, []
    avg = sum(d["delta"] for d in deltas) / len(deltas)
    gained = sorted((d for d in deltas if d["delta"] > 0),
                    key=lambda d: -d["delta"])[:_TOP_N]
    return round(avg, 4), gained


def _duration_minutes(seconds: int) -> int:
    return round(seconds / 60) if seconds else 0


def build_report(username: str, course: str, offset: int = 0,
                 with_comment: bool = True) -> dict:
    """一周的学情快照。纯读，无副作用（LLM 点评除外，它只落埋点）。"""
    c = get_course(course)
    start, end = week_bounds(offset)

    activity = db.window_activity(username, course, start, end)
    errors = db.window_errors(username, course, start, end)
    points = profile_mod.build_profile(username, course)
    delta, gained = _mastery_delta(username, course, start, points)

    # 只点名"碰过的"知识点：build_profile 默认带上全门未练过的点，冷启动时它们
    # 会挤满整张榜，而"你还没学的章节"不是一份周报该给的建议。
    touched = [p for p in points if p["attempts"] or p["error_total"]]
    average = (sum(float(p["mastery"] or 0) for p in points) / len(points)) if points else 0.0

    report = {
        "course": course,
        "course_display": c.display,
        "week_start": start,
        "week_end": (datetime.strptime(end, "%Y-%m-%d").date() - timedelta(days=1)).isoformat(),
        "offset": offset,
        "empty": not activity["attempts"] and not errors["opened"] and not errors["closed"],
        "activity": {
            **activity,
            "duration_minutes": _duration_minutes(activity["duration_seconds"]),
            "correct_rate": round(activity["correct"] / activity["attempts"], 4)
            if activity["attempts"] else None,
        },
        "errors": {
            **errors,
            "error_types": [{**t, "name": name_of(t["error_type"]) or t["error_type"]}
                            for t in errors["error_types"]],
        },
        "mastery": {
            # None 表示"没有可比的基线"，前端据此显示"首次生成"而不是"0.0%"
            "average_delta": delta,
            "has_baseline": delta is not None,
            "average_now": round(average, 4),
            "mastered_points": sum(p["state"] == "mastered" for p in points),
            "total_points": len(points),
            "gained": [{"knowledge_id": g["knowledge_id"], "name": g["name"],
                        "before": g["before"], "after": round(float(g["mastery_raw"]), 4),
                        "delta": g["delta"]} for g in gained],
        },
        "weak_points": [{"knowledge_id": p["knowledge_id"], "name": p["name"],
                         "chapter": p["chapter"], "mastery": p["mastery"],
                         "error_freq": p["error_freq"], "top_error_name": p["top_error_name"],
                         "has_misconception": p["has_misconception"]} for p in touched[:_TOP_N]],
        "cold_start": not touched,
        "comment": "",
        "comment_skipped": "",
    }

    if not with_comment:
        return report
    if report["empty"]:
        # 这周确实什么都没发生。点评一个空周只能靠编。
        report["comment_skipped"] = "no_activity"
        return report
    if not settings.text_llm_ready:
        report["comment_skipped"] = "llm_unavailable"
        return report

    report["comment"] = _comment(report) or ""
    if not report["comment"]:
        report["comment_skipped"] = "llm_failed"
    return report


def _facts(r: dict) -> str:
    """喂给模型的事实清单。只放已经算好的数，不放原始行 —— 给它算的空间就是给它编的空间。"""
    a, m, e = r["activity"], r["mastery"], r["errors"]
    rate = f"{round(a['correct_rate'] * 100)}%" if a["correct_rate"] is not None else "无作答"
    lines = [
        f"科目：{r['course_display']}；周期：{r['week_start']} 至 {r['week_end']}。",
        f"本周作答 {a['attempts']} 次，正确率 {rate}，用时 {a['duration_minutes']} 分钟，"
        f"有学习记录的天数 {a['active_days']} 天，其中用了提示的有 {a['hinted_attempts']} 次。",
        f"本周新增错题 {e['opened']} 道，订正销账 {e['closed']} 道。",
    ]
    if e["error_types"]:
        lines.append("本周错因分布：" + "；".join(
            f"{t['name']} {t['cnt']} 次" for t in e["error_types"][:5]) + "。")
    if m["has_baseline"]:
        lines.append(f"本周全门平均掌握度变化 {m['average_delta']:+.3f}"
                     f"（当前平均 {m['average_now']:.2f}）。")
        if m["gained"]:
            lines.append("涨得最多的知识点：" + "；".join(
                f"{g['name']} {g['before']:.2f}→{g['after']:.2f}" for g in m["gained"]) + "。")
    else:
        lines.append("这是第一次生成周报，没有上周的掌握度基线可比，"
                     "不要提「提升了多少」或「和上周相比」。")
    if r["weak_points"]:
        lines.append("当前最该补的知识点（已按优先级排好，建议就从这几个里挑）：" + "；".join(
            f"{p['name']}（掌握度 {p['mastery']:.2f}"
            + (f"，未订正错题 {p['error_freq']} 道" if p["error_freq"] else "")
            + (f"，主要错因{p['top_error_name']}" if p["top_error_name"] else "")
            + ("，存在概念性错误" if p["has_misconception"] else "") + "）"
            for p in r["weak_points"]) + "。")
    return "\n".join(lines)


def _comment(r: dict) -> str:
    messages = [{"role": "system", "content": REPORT_SYSTEM},
                {"role": "user", "content": _facts(r)}]
    try:
        raw = llm_mod.ask_workhorse(messages, max_tokens=600)
    except Exception:  # noqa: BLE001
        # 周报的主体是数字，模型挂了不该让整页打不开（与各 agent 同风格）。
        return ""
    telemetry.log_llm_call("weekly_report", messages, raw)
    return raw.strip()
