"""GRPO 奖励函数（RLVR）——用项目已有的确定性验证器给 solve 采样打分。

trl 0.24 契约：reward_func(prompts, completions, **reward_kwargs) → list[float | None]，
其中 dataset 每个非 prompt 列作为「逐样本 list」传入 kwargs；返回 None → trainer 以 NaN
语义自动排除该样本（不当 0 分惩罚）——正对应验证器三态 passed=None（无法判定）。

本轮范围 solve-only、格式优先。三函数配 reward_weights=[1.0, 1.0, 0.3]：
  reward_format  —— 主驱动，恒可判，加性 [0,1]（JSON 可解析 + 字段完整 + no-think + 占满整段）
  reward_correct —— check_numeric 正确性；passed=None → None；JSON 不可解析 → 0.0
  reward_shape   —— 稠密 shaping（ds-algo 代码复杂度自洽）；无信号 → None

复用（不重造）：scripts.test_lora._extract_json（括号配平抠 JSON）、
backend.verification.numeric_checker.check_numeric、complexity_checker.check_complexity。
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from scripts.test_lora import _extract_json  # 括号配平 JSON 抽取，比 jsonio 稳
from backend.verification.numeric_checker import check_numeric
from backend.verification.complexity_checker import check_complexity


def _text(completion) -> str:
    """兼容标准(str)与会话式(list[{"role","content"}])两种 completion 形态。"""
    if isinstance(completion, list):
        return completion[0].get("content", "") if completion else ""
    return completion or ""


def _load_canonical(raw):
    """canonical 以 JSON 字符串随 dataset 列携带（规避 Arrow 混合类型）；宽容解析。"""
    if raw is None or isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def reward_format(prompts, completions, gold_keys=None, **kw):
    """格式奖励——solve 短板主驱动，恒可判、绝不返回 None。加性 [0,1]。"""
    n = len(completions)
    gks = gold_keys if gold_keys is not None else [None] * n
    out = []
    for comp, gk in zip(completions, gks):
        t = _text(comp)
        pred = _extract_json(t)
        r = 0.0
        if isinstance(pred, dict):
            r += 0.5                                            # 可解析为 JSON 对象
            if gk and set(gk).issubset(set(pred)):
                r += 0.3                                        # 字段完整（覆盖 gold 全部 key）
            ls = t.lstrip()
            if ls.startswith("{") and "<think>" not in t and "```" not in t:
                r += 0.1                                        # no-think 不变量、无围栏、无前导散文
            if t.strip().endswith("}"):
                r += 0.1                                        # JSON 占满整段（反啰嗦/反截断）
        out.append(r)
    return out


def reward_correct(prompts, completions, intent=None, code_enabled=None,
                   canonical=None, **kw):
    """正确性奖励——check_numeric（无 teacher 依赖）。passed=None → None（排除）。"""
    n = len(completions)
    intents = intent if intent is not None else ["solve"] * n
    codes = code_enabled if code_enabled is not None else [False] * n
    cans = canonical if canonical is not None else [None] * n
    out = []
    for comp, it, ce, craw in zip(completions, intents, codes, cans):
        pred = _extract_json(_text(comp))
        if not isinstance(pred, dict):
            out.append(0.0 if it in ("solve", "diagnose") else None)   # 不可解析=可判定的失败
            continue
        if it == "solve" and not ce:                                    # co/os/net：数值/选择校验
            vr = check_numeric(str(pred.get("final_answer", "")), _load_canonical(craw))
            out.append(None if vr.passed is None else (1.0 if vr.passed else 0.0))
        else:
            out.append(None)                                            # ds-algo solve 无 tests → 交给 shape
    return out


def reward_shape(prompts, completions, intent=None, code_enabled=None, **kw):
    """稠密 shaping——ds-algo 代码复杂度声明自洽（AST 启发式）。无信号 → None。"""
    n = len(completions)
    intents = intent if intent is not None else ["solve"] * n
    codes = code_enabled if code_enabled is not None else [False] * n
    out = []
    for comp, it, ce in zip(completions, intents, codes):
        pred = _extract_json(_text(comp))
        if isinstance(pred, dict) and it == "solve" and ce and pred.get("code"):
            vr = check_complexity(pred.get("code", ""), str(pred.get("complexity", "")))
            out.append(None if vr.passed is None else (1.0 if vr.passed else 0.0))
        else:
            out.append(None)
    return out


REWARD_FUNCS = [reward_format, reward_correct, reward_shape]
REWARD_WEIGHTS = [1.0, 1.0, 0.3]
