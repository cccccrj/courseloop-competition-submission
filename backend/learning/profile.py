"""薄弱知识点画像 —— 个性化练习与复习计划的**唯一**共享取数入口。

## 为什么要有这一层

两个功能都要回答同一个问题："这个学生在哪些知识点上弱、久没练、错得多"。
在此之前它们各答各的：练习生成端点根本不查库（连身份都拿不到），复习计划虽然
查了掌握度，却把 error_freq 与 days_since 硬编码成 0 —— 于是
spaced_review.priority() 的三项因子退化成只看掌握度。

两处各自实现同一件事，迟早给出不一致的答案："复习计划说你最该补『树』，练习页
却一直推『排序』"是很难解释的。所以收口到这里，两边取同一份排序。

## 分层纪律

db.py 出原始聚合（纯 SQL），本模块做衰减与评分（纯算法 + 一次取数），
不碰 LLM。docs/04 把复习排期列为确定性本地能力，这条线不能破。
"""
from __future__ import annotations

import math
import time
from datetime import datetime

from backend.core import db
from backend.core.courses import get_course
from backend.learning import spaced_review
from backend.learning.error_taxonomy import MISCONCEPTION_TAGS, name_of
from backend.learning.mastery import apply_forgetting, derive_state

# 掌握度 → 下一题该出多难。四门通用，与具体课程无关。
_DIFFICULTY_BANDS = ((0.30, 1), (0.50, 2), (0.70, 3), (0.85, 4))

# 知识点不在大纲章节里时的排序位次。库里有几十个运行时建的空壳点（名字来自
# 生成题的细粒度知识点，如「CPI计算」），给个大数让它们天然排在正式章节之后。
_NO_CHAPTER = 99


def _parse_ts(s: str | None) -> float | None:
    """库里的时间戳是 '%Y-%m-%d %H:%M:%S' 本地时间（db._now）。解析失败当没有。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None


def _attempts_from_confidence(conf: float) -> int:
    """由 confidence 反解作答次数。

    历史数据里有一批 mastery 记录来自 TutorGraph 的自由问答，那条路径写 attempt
    时 problem_id 恒为 0，SQL 按题关联就数不到。但 confidence 是当时按
    mastery.confidence(n)=1-e^(-n/3) 算出来的，可以反解回去：n = -3·ln(1-conf)。
    不这么做的话，这些老记录会被 derive_state 判成 not_started —— 明明练过。
    """
    conf = min(max(conf or 0.0, 0.0), 0.949)
    return math.ceil(-3.0 * math.log(1.0 - conf)) if conf > 0 else 0


def _target_difficulty(mastery: float) -> int:
    for ceiling, level in _DIFFICULTY_BANDS:
        if mastery < ceiling:
            return level
    return 5


def build_profile(username: str, course: str, *, include_untouched: bool = True,
                  limit: int = 0, now_ts: float | None = None) -> list[dict]:
    """该生该门全部知识点的画像，按 priority 降序。纯读，无副作用。

    include_untouched=False 时只留"碰过的"（有作答/错题/掌握度），供
    "错题驱动"的场景用；默认带上未练过的，冷启动时才有东西可推。
    """
    now = now_ts if now_ts is not None else time.time()
    rows = db.knowledge_stats(username, course)
    err_types = db.error_types_by_knowledge(username, course, open_only=True)

    chapter_idx = {ch["chapter"]: i
                   for i, ch in enumerate(get_course(course).knowledge_taxonomy)}

    out: list[dict] = []
    for r in rows:
        touched = bool(r["attempts"] or r["error_total"] or r["mastery"] is not None)
        if not include_untouched and not touched:
            continue

        # 最近一次接触：练过、错过、被判过，取最晚的那次
        last = max((t for t in (_parse_ts(r["last_practiced_at"]), _parse_ts(r["last_at"]),
                                _parse_ts(r["last_err_at"])) if t is not None), default=None)
        days_since = round(max(0.0, (now - last) / 86400.0), 2) if last else None

        mastery_raw = r["mastery"] if r["mastery"] is not None else 0.0
        # apply_forgetting 在此之前从未被外部调用过 —— 掌握度只涨不衰，
        # 一个月没碰的知识点和昨天刚练的看起来一样好。
        mastery_eff = apply_forgetting(mastery_raw, days_since or 0.0)

        attempts = r["attempts"] or 0
        if not attempts and r["confidence"]:
            attempts = _attempts_from_confidence(r["confidence"])

        tags = err_types.get(r["knowledge_id"], [])
        top_error = tags[0][0] if tags else ""
        has_misc = any(code in MISCONCEPTION_TAGS and n >= 2 for code, n in tags) or \
            (top_error in MISCONCEPTION_TAGS)

        recent_correct = bool(r["last_score"] is not None and r["last_score"] >= 60)

        out.append({
            "knowledge_id": r["knowledge_id"], "name": r["name"], "chapter": r["chapter"] or "",
            # 有掌握度评估的点（判过题 / 解过题 / 摸底测过）。平均掌握度只对这些点求
            # （见 summarize_mastery）。只有一条错题、没有评估的点不算：它没有掌握度可言，
            # 按 0 算会把"收了一条错题"说成"完全不会"。
            "practiced": r["mastery"] is not None,
            "mastery_raw": round(mastery_raw, 4), "mastery": mastery_eff,
            "confidence": round(r["confidence"] or 0.0, 3),
            "attempts": attempts, "correct_n": r["correct_n"] or 0,
            "recent_correct": recent_correct, "days_since": days_since,
            # priority 吃的是 error_freq，喂未解决的那部分：已经改对的错题不该
            # 一直把这个点顶在最前面，否则"复习过了"在排序上看不出任何变化。
            "error_freq": r["error_open"] or 0, "error_total": r["error_total"] or 0,
            "top_error_type": top_error, "top_error_name": name_of(top_error) if top_error else "",
            "has_misconception": has_misc,
            "bank_count": r["bank_count"] or 0,
            "target_difficulty": _target_difficulty(mastery_eff),
            "review_stage": r["review_stage"] or 0, "next_due_at": r["next_due_at"] or "",
            # 同分时的次级排序位；也被 spaced_review.rank_key 读走，两处共用一把尺子
            "chapter_index": chapter_idx.get(r["chapter"] or "", _NO_CHAPTER),
        })

    _mark_prereq(out)

    for row in out:
        # derive_state 内部自己会调 apply_forgetting（mastery.py），所以这里必须传
        # **未衰减**的 mastery_raw + days_since。传衰减后的值会衰减两次，
        # 让久未练的知识点凭空掉一大截。
        row["state"] = derive_state(row["mastery_raw"], row["attempts"], row["recent_correct"],
                                    row["has_misconception"], days_since=row["days_since"] or 0.0)
        row["priority"] = spaced_review.priority(
            row["mastery"], row["error_freq"], row["days_since"] or 0.0,
            row["is_prereq"], row["attempts"])

    # 与 build_plan 共用 rank_key：两个功能对"谁最该补"必须给出同一个答案
    out.sort(key=spaced_review.rank_key)
    return out[:limit] if limit else out


def _mark_prereq(rows: list[dict]) -> None:
    """标记"是后面某个薄弱点的前置知识"。

    设计文档里有一张 knowledge_prerequisites 表，但从没建过 —— 而且 408 大纲本身
    只有「章 → 知识点」两层，没有先修信息。凭空造一张先修图不如用大纲顺序诚实：
    章节顺序本来就是教学顺序，排在薄弱点之前的章节就是它的前置。

    这么做让 spaced_review.priority() 的 is_prereq(+0.5) 分支第一次真正生效 ——
    在此之前没有任何调用方传过这个参数。


    锚点必须是**有证据的**薄弱点（练过或错过），不能只看 mastery < 0.6 ——
    没练过的点掌握度也是 0，全都算薄弱的话，最深的那个薄弱点就是最后一章，
    于是整门课除末章外人人都是"前置"，+0.5 退化成一个常数，等于没有这个信号。
    """
    weak = [r["chapter_index"] for r in rows
            if r["mastery"] < 0.6 and r["chapter_index"] < _NO_CHAPTER
            and (r["attempts"] or r["error_total"])]
    deepest = max(weak, default=-1)
    for r in rows:
        r["is_prereq"] = r["chapter_index"] < deepest


def summarize_mastery(points: list[dict]) -> dict:
    """平均掌握度的**唯一口径**（测试报告 I-03）。

    学生首页、错题与学习记录、助教的学生详情 / 热力图 / 需要留意的学生，全部用它。
    此前三处各算各的：首页按全部知识点平均（没练过的按 0），记录页按练过的点平均
    且不算遗忘衰减，热力图只对屏幕上那几列求平均 —— 同一个学生 5.6% / 56.3% 并存。

    口径：只对**练过的**知识点求平均（没练过不等于不会，按 0 算会让刚开始学的人
    永远是个位数），掌握度用带遗忘衰减的有效值；覆盖面单独给出 practiced/total，
    页面上和平均值写在一起，不让一个数字假装代表全部。
    一个点都没练过时 average 为 None，页面显示"—"而不是 0%。
    """
    practiced = [p for p in points if p.get("practiced")]
    avg = (sum(float(p["mastery"] or 0) for p in practiced) / len(practiced)) if practiced else None
    return {
        "average": round(avg, 4) if avg is not None else None,
        "practiced": len(practiced),
        "total": len(points),
        "mastered": sum(p["state"] == "mastered" for p in points),
    }


def mastery_summary(username: str, course: str) -> dict:
    return summarize_mastery(build_profile(username, course))


def practiced_points(points: list[dict]) -> list[dict]:
    """练过的知识点，按有效掌握度升序（最弱的在前）。各页"知识点掌握度"列表用它。"""
    rows = [{"knowledge_id": p["knowledge_id"], "knowledge_name": p["name"],
             "mastery": p["mastery"], "state": p["state"], "attempts": p["attempts"]}
            for p in points if p.get("practiced")]
    rows.sort(key=lambda r: (r["mastery"], r["knowledge_id"]))
    return rows


def point_of(username: str, course: str, *, knowledge_id: int = 0, name: str = "") -> dict | None:
    """单个知识点的画像。出题时取错因与目标难度用。"""
    if not knowledge_id and not name:
        return None
    for r in build_profile(username, course):
        if (knowledge_id and r["knowledge_id"] == knowledge_id) or \
           (not knowledge_id and r["name"] == name):
            return r
    return None
