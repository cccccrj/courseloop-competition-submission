"""智增增各模型计费(¥/1k tokens)——横评成本核算用。

★近似值，非官方精确价：请对照智增增实际价目表校准后再下最终成本结论。
统一用人民币(¥)。本地方案边际成本记 0（本机算力免费），另记安装/延迟。
"""
from __future__ import annotations

# (输入价, 输出价) ¥ / 1k tokens —— 近似，可校准
PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro":   (0.0125, 0.100),
    "gemini-2.5-flash": (0.0022, 0.018),
    "gpt-4o":           (0.0180, 0.072),
    "qwen-vl-max":      (0.0045, 0.012),
    "deepseek-v3":      (0.0010, 0.002),   # LLM-judge 用
}
LOCAL_METHODS = {"pp-ocr", "pp-structurev3", "mineru", "got-ocr2"}  # 边际成本 0


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """返回该次调用的 ¥ 成本；未知模型/本地方案返回 0。"""
    if model not in PRICES:
        return 0.0
    pin, pout = PRICES[model]
    return prompt_tokens / 1000 * pin + completion_tokens / 1000 * pout
