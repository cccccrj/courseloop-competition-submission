"""概念/边界规则校验（用于手工模拟题、概念题、算法设计题的确定性辅助）。

不依赖 LLM，做可解释的关键点覆盖检查：
    - must_include：答案应包含的关键概念/步骤（rubric 得分点）
    - must_not    ：不应出现的典型错误说法（misconception 反例）
    - boundaries  ：应考虑的边界情况（空表/单节点/越界/重复…）
返回逐项命中情况，供诊断定位缺失点。
"""
from __future__ import annotations

from backend.verification.base import VerifyResult

# DS&A 常见边界清单（供出题/诊断提醒）
COMMON_BOUNDARIES = ["空输入", "单元素", "越界", "重复元素", "负数", "极大规模", "初始条件", "自环/重边"]


def _contains(text: str, key: str) -> bool:
    return key.lower() in (text or "").lower()


def check_concept(answer: str, must_include: list[str] | None = None,
                  must_not: list[str] | None = None,
                  boundaries: list[str] | None = None) -> VerifyResult:
    must_include = must_include or []
    must_not = must_not or []
    boundaries = boundaries or []

    hit = [k for k in must_include if _contains(answer, k)]
    miss = [k for k in must_include if not _contains(answer, k)]
    bad = [k for k in must_not if _contains(answer, k)]
    missing_bounds = [b for b in boundaries if not _contains(answer, b)]

    ok = (not miss) and (not bad)
    parts = []
    if must_include:
        parts.append(f"关键点 {len(hit)}/{len(must_include)}")
    if miss:
        parts.append("缺失：" + "、".join(miss))
    if bad:
        parts.append("疑似错误说法：" + "、".join(bad))
    if missing_bounds:
        parts.append("未提及边界：" + "、".join(missing_bounds))
    details = "；".join(parts) or "无可检查的规则项"

    passed: bool | None
    if not must_include and not must_not:
        passed = None          # 无规则可判 -> 安全边界
    else:
        passed = ok
    return VerifyResult("rule", passed, details, confidence=0.6,
                        extra={"hit": hit, "miss": miss, "bad": bad, "missing_boundaries": missing_bounds})
