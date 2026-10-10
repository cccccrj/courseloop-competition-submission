"""复习计划 agent：由学习记录驱动生成阶段性复习计划（间隔复习 + LLM 说明）。"""
from __future__ import annotations

from backend.core import llm as llm_mod
from backend.core.config import settings
from backend.core.courses import exam_structure, get_course
from backend.agents.prompts import build_review_system
from backend.learning.spaced_review import build_plan


def summarize(knowledge_states: list[dict], days_to_exam: int = 14,
              daily_minutes: int = 60, goal: str = "", course: str = "ds-algo") -> str:
    """复习安排的自然语言说明。两条排期路径（定额 / 时间预算）共用。

    daily_minutes 由调用方给"实际排出来的日均值"，不是用户填的原值 ——
    预算路径下两者相差一个填充率，提示词里报原值会与学生看到的任务量对不上。
    """
    if not (settings.text_llm_ready and knowledge_states):
        return ""
    c = get_course(course)
    desc = "；".join(f"{k.get('name')}(掌握度{round(k.get('mastery',0),2)},错{k.get('error_freq',0)}次)"
                     for k in knowledge_states[:12])
    # 考研化：告知该门 408 分值与题型，让 LLM 按分值/题型权重安排
    user = (f"本门：{c.display}，在 408 中占 {c.score} 分；试卷结构：{exam_structure()}。\n"
            f"学习目标：{goal or '按分值权重巩固薄弱知识点、冲刺高分'}；距考研初试 {days_to_exam} 天，每天 {daily_minutes} 分钟。\n"
            f"各知识点情况：{desc}\n"
            "请给出简洁的复习安排说明：先复习哪些、为什么，并结合该门分值与选择/综合题型给出侧重建议。")
    try:
        return llm_mod.ask_workhorse([{"role": "system", "content": build_review_system(c)},
                                      {"role": "user", "content": user}])
    except Exception:  # noqa: BLE001
        return ""


def plan(knowledge_states: list[dict], days_to_exam: int = 14,
         daily_minutes: int = 60, goal: str = "", course: str = "ds-algo") -> dict:
    """knowledge_states: [{knowledge_id,name,mastery,error_freq,days_since,is_prereq}]"""
    c = get_course(course)
    tasks = build_plan(knowledge_states, days_to_exam, daily_minutes)
    summary = summarize(knowledge_states, days_to_exam, daily_minutes, goal, course)

    total_days = max((t["day"] for t in tasks), default=0)
    return {"days_to_exam": days_to_exam, "daily_minutes": daily_minutes,
            "span_days": total_days, "tasks": tasks, "summary": summary,
            "course": course, "course_score": c.score, "exam_structure": exam_structure()}
