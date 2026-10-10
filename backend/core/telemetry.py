"""LLM 请求埋点（SFT / 学习分析数据底座）。

把每次 LLM 调用落成一行 JSONL：{时间, intent, 输入 messages, 原始输出, 解析后 JSON, 验证结果, 是否被助教修订}。
配合项目的确定性验证器，可自动筛「通过验证的高质量样本」用于日后蒸馏 SFT（见 docs/05 第六节）。

写日志异常一律吞掉，绝不影响主流程（与各 agent 落库同风格）。
"""
from __future__ import annotations

import json
import os
import time

from backend.core.config import settings

LOG_FILE = "llm_calls.jsonl"


def log_llm_call(intent: str, messages: list[dict], raw_output: str,
                 parsed: dict | None = None, verification: dict | None = None,
                 revised: bool = False, model: str | None = None,
                 usage: dict | None = None) -> None:
    """追加一条 LLM 调用记录到 data/logs/llm_calls.jsonl。

    model 默认按工作模型标注（非流式 agent 均用 ask_workhorse）；流式端点用 reasoner，
    应由调用方显式传入实际模型名。

    `usage` 是这次调用的 token 用量（`llm.chat_raw` 的返回里就有）。之前一条都没记，
    于是"这一批题花了多少钱"只能按"每条多少钱"去估 —— 而估出来的数与实际差多少，
    没有任何办法验证。有 usage 的调用按 token 计价，没有的仍按调用数估，
    `scripts/cost_report.py` 会把两部分分开报，不把估算混进实际里。
    """
    try:
        os.makedirs(settings.logs_dir, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "intent": intent,
            "model": model or settings.workhorse_model,
            "messages": messages,
            "raw_output": raw_output,
            "parsed": parsed,
            "verification": verification,
            "passed": (verification or {}).get("passed") if verification else None,
            "revised": revised,
        }
        # 只在真有用量时才写这个键：`chat_raw` 对不回 usage 的网关返回 None，
        # 写一个全是 null 的 usage 会让报表分不清"网关没给"和"忘了传"。
        if usage and any(v is not None for v in usage.values()):
            record["usage"] = usage
        with open(os.path.join(settings.logs_dir, LOG_FILE), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
