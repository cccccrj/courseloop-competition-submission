"""按调用日志算一批作业花了多少钱。**实测的部分与估算的部分分开报。**

## 起因

上一轮全量补题跑完，被问"这批题花了多少"时只能回答一个区间：
`llm_calls.jsonl` 只记了出题那一路，而一道选择题要问三家复核员，
**复核的调用量是出题的三倍，一条都没落账**。于是账目只能靠
"每条 0.045 元"这个上一轮的经验值去推，而推得准不准无从验证。

复核落账已经补上（`cross_check._ask_one` → `telemetry.log_llm_call`，带 token 用量）。
本脚本把日志汇成一张表。

## 两种口径，不混着报

- **实测**：日志里带 `usage` 的调用，按 token × 单价算。这是真数。
- **估算**：没有 `usage` 的调用（网关不回 usage，或那条记录早于本次改动），
  按"每次调用的经验均价"推。这是估数。

两者分行显示，合计也分开给。混成一个数的坏处很具体：下次有人拿这张表去谈预算，
会把估出来的数当成实测的数用。

## 单价

`PRICES` 是**编辑点**，按你实际使用的网关报价改。默认值只是占位，
标了 `?` 的表示没有可靠报价、按同档模型代入。单价错了整张表就错了 ——
所以表头会把当前用的单价原样打出来，让人一眼看出这张表建立在什么假设上。

## 用法

    python -m scripts.cost_report                      # 全部日志
    python -m scripts.cost_report --since 2026-09-01   # 只看某天起
    python -m scripts.cost_report --intent cross_check # 只看某一路
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import settings  # noqa: E402
from backend.core.telemetry import LOG_FILE  # noqa: E402

# 元 / 百万 token（输入, 输出）。改成你的网关实际报价。
# 带 "?" 的注释表示这一档没有确切报价，按同级模型代入，算出来的是量级不是精确值。
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (1.10, 4.40),
    "gpt-4o": (18.00, 72.00),
    "deepseek-chat": (2.00, 8.00),
    "deepseek-reasoner": (4.00, 16.00),
    "gemini-2.5-flash": (2.20, 18.00),   # ?
}
# 没有 usage 时按每次调用多少钱估。来源：上一轮 3000 条合成的实际账单
# （生成 ¥109.09 / 3000 条 ≈ 0.036，语义校验 ¥26.84 / 3000 ≈ 0.009）。
FALLBACK_PER_CALL = 0.045


def price_of(model: str) -> tuple[float, float] | None:
    if model in PRICES:
        return PRICES[model]
    # 网关常给带前缀的模型名（openai/gpt-4o-mini、us.anthropic.xxx）。
    # 宽松匹配一次，匹配不上就老实按未知处理，不硬套一个价。
    for name, pair in PRICES.items():
        if name in model:
            return pair
    return None


def load(path: Path, since: str = "", intent: str = "") -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue          # 半行/损坏行跳过，不让一行坏数据毁掉整张表
            if since and str(d.get("ts", "")) < since:
                continue
            if intent and d.get("intent") != intent:
                continue
            rows.append(d)
    return rows


def summarize(rows: list[dict]) -> dict:
    """按 (intent, model) 汇总。实测与估算分两组累计。"""
    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"calls": 0, "metered": 0, "prompt": 0, "completion": 0,
                 "measured_cny": 0.0, "unpriced": 0})
    for d in rows:
        key = (d.get("intent") or "?", d.get("model") or "?")
        a = agg[key]
        a["calls"] += 1
        usage = d.get("usage") or {}
        pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if pt is None and ct is None:
            continue
        a["metered"] += 1
        a["prompt"] += int(pt or 0)
        a["completion"] += int(ct or 0)
        pair = price_of(key[1])
        if pair is None:
            a["unpriced"] += 1
            continue
        a["measured_cny"] += (int(pt or 0) * pair[0] + int(ct or 0) * pair[1]) / 1_000_000
    return agg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="", help="只统计这个时间戳之后的（如 2026-09-01）")
    ap.add_argument("--intent", default="", help="只看某一路（practice / cross_check / solve …）")
    ap.add_argument("--log", default="", help="日志路径，默认 data/logs/llm_calls.jsonl")
    args = ap.parse_args()

    path = Path(args.log) if args.log else Path(settings.logs_dir) / LOG_FILE
    if not path.exists():
        print(f"✗ 找不到日志：{path}", file=sys.stderr)
        return 2

    rows = load(path, args.since, args.intent)
    if not rows:
        print("没有符合条件的记录。")
        return 0

    agg = summarize(rows)
    print(f"▶ {path}｜{len(rows)} 条记录"
          + (f"｜{args.since} 起" if args.since else "")
          + (f"｜intent={args.intent}" if args.intent else ""))
    print(f"  单价表（元/百万 token）：" +
          "，".join(f"{k} {v[0]}/{v[1]}" for k, v in PRICES.items()))
    print(f"  无 usage 时按 {FALLBACK_PER_CALL} 元/次估\n")

    head = f"{'intent':<12}{'model':<20}{'调用':>7}{'其中计量':>9}{'输入tok':>10}{'输出tok':>10}{'实测(元)':>11}{'估算(元)':>11}"
    print(head)
    print("-" * len(head))
    tot_measured = tot_est = tot_calls = tot_metered = 0.0
    for (intent, model), a in sorted(agg.items(), key=lambda kv: -kv[1]["calls"]):
        est = (a["calls"] - a["metered"]) * FALLBACK_PER_CALL
        tot_measured += a["measured_cny"]
        tot_est += est
        tot_calls += a["calls"]
        tot_metered += a["metered"]
        flag = "  ← 无单价" if a["unpriced"] else ""
        print(f"{intent:<12}{model:<20}{a['calls']:>7}{a['metered']:>9}"
              f"{a['prompt']:>10}{a['completion']:>10}"
              f"{a['measured_cny']:>11.2f}{est:>11.2f}{flag}")
    print("-" * len(head))
    print(f"{'合计':<32}{int(tot_calls):>7}{int(tot_metered):>9}{'':>20}"
          f"{tot_measured:>11.2f}{tot_est:>11.2f}")
    # 两个数不相加：一个是账，一个是猜。要一个总数的人自己把它们加起来，
    # 但至少他知道自己加的是什么。
    print(f"\n实测 {tot_measured:.2f} 元（{int(tot_metered)} 次带 token 用量）"
          f"｜估算 {tot_est:.2f} 元（{int(tot_calls - tot_metered)} 次无用量，按均价推）")
    if tot_metered == 0:
        print("提示：一条带 usage 的记录都没有 —— 说明这批日志早于复核落账那次改动，"
              "或者网关不回 usage。这张表目前全是估算。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
