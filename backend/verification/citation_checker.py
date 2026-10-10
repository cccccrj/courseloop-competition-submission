"""引用校验（method="citation"）：解题步骤的"依据"是否有教材片段支撑。

轻量、无 LLM：把某一步的 basis（用到的定义/性质/章节）与检索到的教材 chunk 文本做
字符二元组（CJK）+ 词（拉丁）重叠度比对。有支撑→True；命中低→False；无语料→None（安全边界）。
用于给 solver 每步 basis 标注"是否可溯源"，或在评测里量化"讲解有据"。

    from backend.verification.citation_checker import check_citation
    vr = check_citation("由二叉搜索树中序遍历有序性", [hit.text for hit in hits])
"""
from __future__ import annotations

import re

from backend.verification.base import VerifyResult

_STOP = set("的了和与及或即如是在对为以中一个这那其之等则可将会被把从并且用到")


def _grams(text: str) -> set[str]:
    """CJK 取相邻二字元组；拉丁取小写词。用于粗粒度语义重叠。"""
    s = (text or "").strip()
    grams: set[str] = set()
    for w in re.findall(r"[a-zA-Z]{2,}", s):
        grams.add(w.lower())
    cjk = re.findall(r"[一-鿿]", s)
    cjk = [c for c in cjk if c not in _STOP]
    for i in range(len(cjk) - 1):
        grams.add(cjk[i] + cjk[i + 1])
    return grams


def check_citation(basis: str, chunks: list[str], threshold: float = 0.34) -> VerifyResult:
    """basis 与检索片段的重叠度 ≥ threshold 视为有教材支撑。chunks 为空→无法判定(None)。"""
    bg = _grams(basis)
    if not chunks:
        return VerifyResult("citation", None, "无检索语料，无法核对依据", confidence=0.0)
    if not bg:
        return VerifyResult("citation", None, "依据为空/无可比对内容", confidence=0.0)
    corpus = _grams("\n".join(chunks))
    hit = bg & corpus
    ratio = len(hit) / len(bg)
    passed = ratio >= threshold
    return VerifyResult("citation", passed,
                        f"依据与教材重叠 {len(hit)}/{len(bg)}（{ratio:.0%}，阈值 {threshold:.0%}）",
                        confidence=round(min(0.9, 0.3 + ratio), 2),
                        extra={"overlap": round(ratio, 3), "matched": sorted(hit)[:12]})


def annotate_steps(steps: list[dict], chunks: list[str]) -> list[dict]:
    """给 solver 的 steps 逐条加 cited 标注（True/False/None）。原地返回 steps。"""
    for s in steps:
        if isinstance(s, dict):
            s["cited"] = check_citation(s.get("basis", ""), chunks).passed
    return steps
