"""统计 data/logs/llm_calls.jsonl —— 这份埋点的第一个消费方。

## 它补的是哪个洞

`backend/core/telemetry.py` 每次 LLM 调用都落一行，带 `parsed` 与 `verification`，
即"真实用户输入 + 模型输出 + 确定性对错标签"的三元组。但在此之前，
只有 `scripts/backup_userdb.py --with-logs` 会把它打包归档，**没有任何脚本读它**
（docs/18 §2.1②）。只写不读的数据等于没有。

本脚本只做统计，不做训练、不做导出增广：先把"这份日志里到底有什么"变成一屏能看的东西。

## 三条口径（改这里之前先读）

1. **自由文本任务没有"JSON 解析失败率"这回事。**
   `solve` / `diagnose` / `practice` 的 agent 会把输出喂给 `jsonio.extract_json`，
   解析不出来才记 `parsed=None`；而 `grounded_qa` / `weekly_report` / `photo_transcribe`
   的埋点调用**恒定**传 `parsed=None`（它们本来就返回自然语言）。
   把两类混在一个分母里，会给教材问答报出一个 100% 的解析失败率 ——
   那是埋点的形状，不是模型的问题。所以这一列对自由文本任务显式留白。

2. **验证结论是三态，`None` 不折成 `False`。**
   与 `backend/verification/` 和 `scripts/grpo_rewards.py` 同一条纪律：
   `None` 是"证据不足、无法判定"（例如无代码可跑的综合题），不是"没通过"。
   通过率的分母因此只含 True/False 两类，未判定的单独成列。
   把未判定并进失败，四门里三门没有可编程判题，通过率会被压到没法看。

3. **耗时没有埋。** `log_llm_call` 的字段里没有耗时，`ts` 只精确到秒且是写日志的时刻，
   相邻两行也不是同一次请求 —— 从这份日志里估不出延迟。本脚本不猜，直接不报这一列。
   要这个指标就得先给埋点加字段（模型对照台走的是 `llm.chat_raw`，那条路才有 `latency_ms`）。

用法：
    python -m scripts.analyze_llm_calls                          # 全量统计
    python -m scripts.analyze_llm_calls --since 2026-08-20       # 只看某天起
    python -m scripts.analyze_llm_calls --intent diagnose
    python -m scripts.analyze_llm_calls --out data/eval/llm_calls_summary.json
"""
from __future__ import annotations

import argparse
import json
import os
import unicodedata
from collections import Counter

from backend.core.config import settings
from backend.core.telemetry import LOG_FILE

# 会把模型输出解析成 JSON 的 intent：这几个的 parsed=None 才是"解析失败"。
JSON_INTENTS = {"solve", "diagnose", "practice"}
# 天生只产自由文本的 intent：parsed 恒为 None，不参与解析失败率。
TEXT_INTENTS = {"grounded_qa", "weekly_report", "photo_transcribe"}


def default_path() -> str:
    return os.path.join(settings.logs_dir, LOG_FILE)


def load(path: str, since: str = "", intent: str = "") -> tuple[list[dict], Counter]:
    """读日志，返回 (记录, 计数)。坏行跳过并计数，不抛。"""
    stat: Counter = Counter()
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            stat["lines"] += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # 埋点写入是"异常一律吞掉"的尽力而为路径，进程被 kill 时最后一行可能只写了一半。
                # 坏行必须报出来：不报的话统计口径会静悄悄地缺一块，而缺多少没人知道。
                stat["bad_lines"] += 1
                continue
            if not isinstance(rec, dict):
                stat["bad_lines"] += 1
                continue
            ts = str(rec.get("ts") or "")
            if since:
                if not ts:
                    # 证不出它在窗口内，就不算进窗口。
                    stat["skip_no_ts"] += 1
                    continue
                if ts[:10] < since:
                    stat["skip_before_since"] += 1
                    continue
            if intent and rec.get("intent") != intent:
                stat["skip_other_intent"] += 1
                continue
            rows.append(rec)
    return rows, stat


def _verdict(rec: dict) -> bool | None:
    """三态验证结论。顶层 passed 是 telemetry 从 verification 抽出来的，优先用它。"""
    if rec.get("passed") is not None:
        return bool(rec["passed"])
    ver = rec.get("verification")
    if isinstance(ver, dict) and ver.get("passed") is not None:
        return bool(ver["passed"])
    return None


def _blank() -> dict:
    return {"n": 0, "parse_ok": 0, "parse_fail": 0, "passed": 0, "failed": 0,
            "unknown": 0, "empty_output": 0, "revised": 0, "sft_ready": 0,
            "chars": 0, "first_ts": "", "last_ts": "", "models": Counter(),
            "json_schema": False}


def _accumulate(bucket: dict, rec: dict, json_schema: bool) -> None:
    bucket["n"] += 1
    bucket["json_schema"] = bucket["json_schema"] or json_schema
    parsed_ok = rec.get("parsed") is not None
    if json_schema:
        bucket["parse_ok" if parsed_ok else "parse_fail"] += 1
    raw = rec.get("raw_output") or ""
    bucket["chars"] += len(raw)
    if not str(raw).strip():
        # 空输出多半是调用炸了：/chat/stream 的异常分支会把已累计的 answer（此时为空）照样埋点。
        bucket["empty_output"] += 1
    if rec.get("revised"):
        bucket["revised"] += 1
    verdict = _verdict(rec)
    bucket["passed" if verdict is True else "failed" if verdict is False else "unknown"] += 1
    if verdict is True and parsed_ok:
        bucket["sft_ready"] += 1
    model = str(rec.get("model") or "(未记录)")
    bucket["models"][model] += 1
    ts = str(rec.get("ts") or "")
    if ts:
        if not bucket["first_ts"] or ts < bucket["first_ts"]:
            bucket["first_ts"] = ts
        if ts > bucket["last_ts"]:
            bucket["last_ts"] = ts


def summarize(rows: list[dict]) -> dict:
    """按 intent / model 两个维度聚合。"""
    # 后来新增的 JSON 任务不会自动出现在 JSON_INTENTS 里。凡是出现过一条成功解析的
    # intent，就按 JSON 任务统计 —— 否则新任务的解析失败率会一直显示留白，
    # 而"这个新任务从来没解析成功过"恰恰是最该被看见的情况。
    json_like = set(JSON_INTENTS)
    for rec in rows:
        if rec.get("parsed") is not None:
            json_like.add(str(rec.get("intent") or ""))

    by_intent: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    for rec in rows:
        name = str(rec.get("intent") or "(未记录)")
        schema = name in json_like
        _accumulate(by_intent.setdefault(name, _blank()), rec, schema)
        _accumulate(by_model.setdefault(str(rec.get("model") or "(未记录)"), _blank()), rec, schema)

    totals = _blank()
    for rec in rows:
        _accumulate(totals, rec, str(rec.get("intent") or "") in json_like)
    unknown_intents = sorted(set(by_intent) - JSON_INTENTS - TEXT_INTENTS - {"(未记录)"})
    return {"totals": totals, "by_intent": by_intent, "by_model": by_model,
            "unregistered_intents": unknown_intents}


def parse_fail_rate(b: dict) -> float | None:
    """自由文本任务返回 None（不适用），不是 0。"""
    if not b["json_schema"] or not b["n"]:
        return None
    return round(100.0 * b["parse_fail"] / (b["parse_ok"] + b["parse_fail"]), 1)


def pass_rate(b: dict) -> float | None:
    """分母只含可判定的（True/False），全未判定时返回 None。"""
    judged = b["passed"] + b["failed"]
    return round(100.0 * b["passed"] / judged, 1) if judged else None


def _w(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _cell(text: str, width: int, right: bool = True) -> str:
    gap = " " * max(0, width - _w(text))
    return gap + text if right else text + gap


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value}%"


_COLS = (("任务", 18, False), ("调用", 7, True), ("解析失败", 11, True),
         ("验证通过率", 13, True), ("通过/失败/未判", 18, True),
         ("空输出", 8, True), ("蒸馏候选", 11, True))


def _cols(names, label: str = "任务") -> tuple:
    """首列宽度跟着最长的名字走：本地 LoRA 的别名比 intent 长得多，写死会顶开整行。"""
    width = max([_COLS[0][1]] + [_w(str(n)) + 2 for n in names])
    return ((label, width, _COLS[0][2]),) + _COLS[1:]


def _row(name: str, b: dict, cols: tuple = _COLS) -> str:
    trio = f"{b['passed']}/{b['failed']}/{b['unknown']}"
    values = (name, str(b["n"]), _pct(parse_fail_rate(b)), _pct(pass_rate(b)),
              trio, str(b["empty_output"]), str(b["sft_ready"]))
    return "  " + "".join(_cell(v, w, r) for v, (_, w, r) in zip(values, cols))


def format_report(summary: dict, path: str, stat: Counter) -> list[str]:
    totals = summary["totals"]
    lines = [f"▶ {path}"]
    span = f"{totals['first_ts'] or '?'} → {totals['last_ts'] or '?'}"
    head = f"  {stat['lines']} 行，统计 {totals['n']} 条    时间跨度 {span}"
    if stat["bad_lines"]:
        head += f"    ⚠ 坏行 {stat['bad_lines']} 已跳过"
    lines.append(head)
    skipped = [(f"{stat[k]} 条{label}") for k, label in
               (("skip_before_since", "早于 --since"), ("skip_no_ts", "没有时间戳"),
                ("skip_other_intent", "非目标 intent")) if stat[k]]
    if skipped:
        lines.append("  已过滤：" + "、".join(skipped))
    if not totals["n"]:
        lines.append("  （窗口内没有记录）")
        return lines

    cols = _cols(summary["by_intent"])
    lines += ["", "  " + "".join(_cell(t, w, r) for t, w, r in cols)]
    for name, b in sorted(summary["by_intent"].items(), key=lambda kv: -kv[1]["n"]):
        lines.append(_row(name, b, cols))
    lines.append(_row("合计", totals, cols))

    if len(summary["by_model"]) > 1:
        cols = _cols(summary["by_model"], "模型")
        lines += ["", "  " + "".join(_cell(t, w, r) for t, w, r in cols)]
        for name, b in sorted(summary["by_model"].items(), key=lambda kv: -kv[1]["n"]):
            lines.append(_row(name, b, cols))

    lines += ["",
              f"  蒸馏候选 {totals['sft_ready']} 条 = 解析成功且确定性验证通过，可直接做 SFT 样本。",
              "  「—」表示不适用：自由文本任务没有 JSON 解析率；一条都没判定时没有通过率。",
              "  未判定 ≠ 失败：证据不足（如无代码可跑的综合题）按无法判定记，不折成 0 分。",
              "  耗时未埋点（telemetry 无该字段），本脚本不估算。"]
    if totals["revised"]:
        lines.append(f"  其中 {totals['revised']} 条被标记为经助教修订。")
    if summary["unregistered_intents"]:
        lines.append("  · 未登记的 intent：" + "、".join(summary["unregistered_intents"])
                     + "  —— 若它也吐 JSON，请加进 JSON_INTENTS")
    return lines


def _jsonable(b: dict) -> dict:
    out = {k: v for k, v in b.items() if k != "models"}
    out["models"] = dict(b["models"])
    out["parse_fail_rate"] = parse_fail_rate(b)
    out["pass_rate"] = pass_rate(b)
    out["avg_output_chars"] = round(b["chars"] / b["n"]) if b["n"] else 0
    return out


def to_json(summary: dict, path: str, stat: Counter) -> dict:
    return {
        "source": path,
        "lines": stat["lines"],
        "bad_lines": stat["bad_lines"],
        "counted": summary["totals"]["n"],
        "totals": _jsonable(summary["totals"]),
        "by_intent": {k: _jsonable(v) for k, v in summary["by_intent"].items()},
        "by_model": {k: _jsonable(v) for k, v in summary["by_model"].items()},
        "unregistered_intents": summary["unregistered_intents"],
        "note": "耗时未埋点；验证结论三态，未判定不计入通过率分母",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="统计 LLM 调用埋点（解析失败率 / 验证通过率 / 蒸馏候选）")
    ap.add_argument("--file", default="", help=f"日志路径，默认 {default_path()}")
    ap.add_argument("--since", default="", help="只统计该日期起的记录，形如 2026-08-20")
    ap.add_argument("--intent", default="", help="只看某个任务：solve / diagnose / practice / grounded_qa ...")
    ap.add_argument("--out", default="", help="把汇总写成 JSON（不给就只打印）")
    args = ap.parse_args()

    path = os.path.abspath(args.file or default_path())
    if not os.path.exists(path):
        print(f"× 找不到 {path}")
        print("  埋点由后端在真实调用时写入；这台机器上没跑过就不会有这个文件。")
        return 1

    rows, stat = load(path, since=args.since, intent=args.intent)
    summary = summarize(rows)
    print("\n".join(format_report(summary, path, stat)))

    if args.out:
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(to_json(summary, path, stat), f, ensure_ascii=False, indent=2)
        print(f"\n✓ 汇总已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
