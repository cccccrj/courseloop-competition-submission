"""知识点掌握度模型与状态机。

掌握度 = f(正确率, 难度修正, 最近表现, 提示依赖度, 遗忘衰减, 教师确认)
状态：not_started / learning / fragile / mastered / needs_review / misconception
持久化到 mastery_records(username, knowledge_id, mastery, confidence, state, last_practiced_at)。
"""
from __future__ import annotations

import math

STATES = ["not_started", "learning", "fragile", "mastered", "needs_review", "misconception"]

# 遗忘衰减半衰期（天）：越久没练，掌握度按指数衰减
FORGET_HALF_LIFE_DAYS = 14.0


def _clamp(x: float, lo=0.0, hi=1.0) -> float:
    return max(lo, min(hi, x))


def update_mastery(prev: float, correct: bool, difficulty: int = 2,
                   hints_used: int = 0, is_misconception: bool = False,
                   teacher_confirmed: bool = False) -> float:
    """单次作答后更新掌握度（0~1）。

    - 答对：按难度加分（难题加得多），提示越多加得越少
    - 答错：扣分，稳定错误概念扣更多
    - 教师确认正确：额外加成
    """
    prev = _clamp(prev)
    diff_factor = 0.6 + 0.2 * _clamp(difficulty / 5)     # 难度 1~5 -> 0.64~0.8
    hint_penalty = 1.0 - _clamp(hints_used * 0.15, 0, 0.6)  # 提示依赖度

    if correct:
        gain = 0.22 * diff_factor * hint_penalty
        if teacher_confirmed:
            gain += 0.08
        new = prev + gain * (1 - prev)          # 越接近满分涨得越慢
    else:
        drop = 0.28 if is_misconception else 0.18
        new = prev - drop * (0.4 + prev)        # 掌握度越高错一次扣得越多（惩罚 fragile）
    return round(_clamp(new), 4)


def apply_forgetting(mastery: float, days_since: float) -> float:
    """遗忘衰减：距上次练习 days_since 天后的当前掌握度。"""
    if days_since <= 0:
        return mastery
    decay = math.pow(0.5, days_since / FORGET_HALF_LIFE_DAYS)
    # 只衰减「超过稳固线 0.3 的部分」，保留基础记忆
    return round(0.3 * mastery + 0.7 * mastery * decay, 4)


def derive_state(mastery: float, attempts: int, recent_correct: bool,
                 has_misconception: bool, days_since: float = 0.0) -> str:
    """由掌握度与近期表现推导学习状态。mastery 传**未衰减**的原值，衰减在函数内部做。"""
    if attempts == 0:
        return "not_started"
    if has_misconception:
        return "misconception"
    # 「曾经掌握过」看未衰减的原值，「现在还剩多少」才看衰减后的值。
    # 原先两个判断都用衰减值，导致 needs_review 分支**数学上不可达**：
    # 要 eff>=0.8 且 days>14，代入 eff=m(0.3+0.7·0.5^(d/14)) 需要 m>1.26。
    # 于是六态状态机实际只有五态可用，偏偏缺的是复习产品最需要的那一态。
    if mastery >= 0.8:
        return "needs_review" if days_since > FORGET_HALF_LIFE_DAYS else "mastered"
    eff = apply_forgetting(mastery, days_since)
    if eff >= 0.55:
        return "fragile" if not recent_correct else "learning"
    return "learning"


def confidence(attempts: int) -> float:
    """作答次数越多，掌握度估计越可信。"""
    return round(_clamp(1 - math.exp(-attempts / 3)), 3)
