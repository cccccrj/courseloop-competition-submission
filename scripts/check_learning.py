"""learning/ 纯函数的行为断言。退出码非 0 即失败。

仓库没有 pytest（requirements.txt 里也没有），与既有的 scripts/evaluate.py +
data/eval/ 一个范式：可执行脚本 + 断言 + 退出码，不引入新依赖。

这里锁的都是**改一行就会静悄悄错掉、但页面上看不出来**的行为：

  · 遗忘衰减必须真的衰减，且久未练一定不高于刚练过（apply_forgetting 在这次改动
    之前从未被外部调用，等于没生效过）
  · derive_state 不能双重衰减 —— 它内部自己调 apply_forgetting，调用方再传一个
    已衰减的值进去，久未练的知识点会凭空掉两次
  · "没练过"必须排在"练了但答错"之后 —— 否则"根据错题推荐练习"根本不成立
  · is_prereq 不能人人有份，那样 +0.5 就退化成常数
  · 间隔序列必须是 1/3/7/14/30，答错回退到最短

用法：
    python -m scripts.check_learning
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.learning.mastery import (  # noqa: E402
    STATES, apply_forgetting, confidence, derive_state, update_mastery)
from backend.learning.spaced_review import (  # noqa: E402
    BASE_INTERVALS, build_plan, next_interval, priority, rank_key)

_fails: list[str] = []


def ok(cond: bool, name: str, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{'' if cond else f'  —— {detail}'}")
    if not cond:
        _fails.append(name)


def check_forgetting() -> None:
    print("▶ 遗忘衰减")
    m = 0.8
    ok(apply_forgetting(m, 0) == m, "0 天不衰减")
    seq = [apply_forgetting(m, d) for d in (0, 7, 14, 30, 90)]
    ok(all(a >= b for a, b in zip(seq, seq[1:])), "天数越久掌握度单调不增", str(seq))
    ok(apply_forgetting(m, 14) < m, "半衰期后确实掉了", str(apply_forgetting(m, 14)))
    ok(apply_forgetting(0.0, 90) == 0.0, "零掌握度衰减仍是零")
    # 只衰减超过稳固线的部分，不该归零：学过的东西不会忘得一干二净
    ok(apply_forgetting(m, 3650) >= 0.3 * m, "极久之后仍保留基础记忆",
       str(apply_forgetting(m, 3650)))


def check_no_double_decay() -> None:
    print("▶ derive_state 不双重衰减")
    raw, days = 0.85, 30.0
    # 正确用法：传未衰减的原值 + 天数，让 derive_state 自己衰减一次
    right = derive_state(raw, attempts=5, recent_correct=True,
                         has_misconception=False, days_since=days)
    # 错误用法：调用方先衰减了再传进去，等于衰减两次
    wrong = derive_state(apply_forgetting(raw, days), attempts=5, recent_correct=True,
                         has_misconception=False, days_since=days)
    ok(right == "needs_review", "久未练的高掌握度 → needs_review", right)
    ok(wrong == "learning", "先衰减再传会掉到 learning（证明这个坑是真的）",
       f"right={right} wrong={wrong}")
    # 六态必须都可达。needs_review 曾因判据用了衰减后的值而数学上不可能触发。
    reachable = {
        derive_state(0.0, 0, False, False),                              # not_started
        derive_state(0.5, 3, False, True),                               # misconception
        derive_state(0.85, 5, True, False, days_since=0.0),              # mastered
        derive_state(0.85, 5, True, False, days_since=30.0),             # needs_review
        derive_state(0.60, 4, False, False, days_since=0.0),             # fragile
        derive_state(0.20, 2, False, False, days_since=0.0),             # learning
    }
    ok(reachable == set(STATES), "六个状态全部可达", f"缺 {set(STATES) - reachable}")
    ok(derive_state(0.85, 5, True, False, days_since=0.0) == "mastered",
       "刚练过的高掌握度 → mastered")
    ok(derive_state(0.0, 0, False, False) == "not_started", "零作答 → not_started")
    ok(derive_state(0.5, 3, False, True) == "misconception", "有稳定概念错误 → misconception")


def check_priority_ordering() -> None:
    print("▶ 优先级排序")
    wrong_once = {"mastery": 0.0, "error_freq": 1, "days_since": 0, "attempts": 1,
                  "knowledge_id": 2, "chapter_index": 5}
    untouched = {"mastery": 0.0, "error_freq": 0, "days_since": 0, "attempts": 0,
                 "knowledge_id": 1, "chapter_index": 0}
    p_wrong = priority(**{k: v for k, v in wrong_once.items()
                          if k in ("mastery", "error_freq", "days_since", "attempts")})
    p_untouched = priority(**{k: v for k, v in untouched.items()
                              if k in ("mastery", "error_freq", "days_since", "attempts")})
    ok(p_wrong > p_untouched, "答错过的排在从没练过的前面", f"{p_wrong} vs {p_untouched}")
    ok(sorted([untouched, wrong_once], key=rank_key)[0] is wrong_once,
       "rank_key 与 priority 同向")

    # 冷启动：全门同分时按章节顺序，从第一章开始才合理
    cold = [{"mastery": 0.0, "error_freq": 0, "days_since": 0, "attempts": 0,
             "knowledge_id": i, "chapter_index": c} for i, c in ((7, 3), (8, 0), (9, 1))]
    ok([r["chapter_index"] for r in sorted(cold, key=rank_key)] == [0, 1, 3],
       "冷启动同分时按章节顺序")

    ok(priority(0.0, 0, 0, attempts=5) > priority(0.0, 0, 0, attempts=0),
       "练过但仍不会 > 从没练过")
    ok(priority(0.2, 3, 0) > priority(0.2, 0, 0), "错题多的排更前")
    ok(priority(0.5, 0, 60) > priority(0.5, 0, 0), "久未练的排更前")
    ok(priority(0.5, 0, 0, is_prereq=True) > priority(0.5, 0, 0), "前置知识排更前")
    # 老调用方不传 attempts 时行为必须与改动前一致
    ok(priority(0.4, 1, 10) == priority(0.4, 1, 10, attempts=1), "attempts 默认值向后兼容")


def check_intervals() -> None:
    print("▶ 间隔复习")
    ok(BASE_INTERVALS == [1, 3, 7, 14, 30], "基础间隔 1/3/7/14/30", str(BASE_INTERVALS))
    stage, seen = 0, []
    for _ in range(6):
        interval, stage = next_interval(stage, correct=True, hints_used=0)
        seen.append(interval)
    ok(seen == [3, 7, 14, 30, 30, 30], "连续答对逐级拉长并封顶", str(seen))
    ok(next_interval(3, correct=False) == (1, 0), "答错回退到最短间隔",
       str(next_interval(3, correct=False)))
    ok(next_interval(2, correct=True, hints_used=3)[1] == 2, "依赖提示则原地巩固不进阶")

    states = [
        {"knowledge_id": 1, "name": "薄弱点", "mastery": 0.2, "error_freq": 2,
         "days_since": 10, "attempts": 3, "review_stage": 0, "chapter_index": 0},
        {"knowledge_id": 2, "name": "未学习", "mastery": 0, "error_freq": 0,
         "days_since": 0, "attempts": 0, "review_stage": 0, "chapter_index": 1},
    ]
    tasks = build_plan(states, days_to_exam=30, daily_minutes=15)
    ok([t["knowledge_id"] for t in tasks[:3]] == [1, 1, 1],
       "薄弱点按 1/3/7 节奏重复安排", str([(t["knowledge_id"], t["day"]) for t in tasks]))
    per_day = {day: sum(t["day"] == day for t in tasks) for day in range(1, 31)}
    ok(max(per_day.values()) <= 1, "每日任务不超过时长容量", str(per_day))
    ok(all(t["round"] in (1, 2, 3) and t["interval"] in BASE_INTERVALS for t in tasks),
       "任务持有轮次与间隔元数据")


def check_mastery_update() -> None:
    print("▶ 掌握度更新")
    ok(update_mastery(0.5, True) > 0.5, "答对上涨")
    ok(update_mastery(0.5, False) < 0.5, "答错下跌")
    ok(update_mastery(0.5, False, is_misconception=True) < update_mastery(0.5, False),
       "概念性错误扣更多")
    ok(update_mastery(0.5, True, hints_used=3) < update_mastery(0.5, True, hints_used=0),
       "依赖提示涨得少")
    ok(update_mastery(0.9, True) - 0.9 < update_mastery(0.3, True) - 0.3,
       "越接近满分涨得越慢")
    ok(0.0 <= update_mastery(0.0, False) <= 1.0, "结果不越界")
    ok(confidence(0) == 0.0 and confidence(20) > 0.99, "置信度随作答次数收敛")


def main() -> int:
    for fn in (check_forgetting, check_no_double_decay, check_priority_ordering,
               check_intervals, check_mastery_update):
        fn()
        print()
    print(f"{'✗ ' + str(len(_fails)) + ' 项未通过：' + '、'.join(_fails) if _fails else '✓ 全部通过'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
