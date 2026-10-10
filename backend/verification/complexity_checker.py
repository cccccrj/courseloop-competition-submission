"""复杂度校验：用 AST 粗估代码时间复杂度，与学生/模型的声明比对。

启发式（best-effort，非严格证明）：
    - 最大循环嵌套深度 d -> O(n^d)
    - 出现「折半」模式（//2 / >>1 / mid）且非嵌套 -> 含 log n
    - 递归调用 -> 至少线性，二分递归常见为 O(n log n) / O(log n)（给出提示，不武断）
返回 passed：声明与估计一致=True，明显不符=False，无法判定=None（安全边界）。
"""
from __future__ import annotations

import ast
import re

from backend.verification.base import VerifyResult

_ORDER = ["O(1)", "O(log n)", "O(n)", "O(n log n)", "O(n^2)", "O(n^3)", "O(2^n)"]


def _normalize(claim: str) -> str | None:
    """把自由文本的复杂度声明规范化到 _ORDER 之一。"""
    if not claim:
        return None
    s = claim.lower().replace(" ", "").replace("（", "(").replace("）", ")")
    s = s.replace("logn", "log n").replace("nlogn", "n log n")
    table = {
        "o(1)": "O(1)", "常数": "O(1)",
        "o(logn)": "O(log n)", "o(log n)": "O(log n)", "对数": "O(log n)",
        "o(n)": "O(n)", "线性": "O(n)",
        "o(nlogn)": "O(n log n)", "o(n log n)": "O(n log n)",
        "o(n^2)": "O(n^2)", "o(n²)": "O(n^2)", "o(n2)": "O(n^2)", "平方": "O(n^2)",
        "o(n^3)": "O(n^3)", "立方": "O(n^3)",
        "o(2^n)": "O(2^n)", "指数": "O(2^n)",
    }
    for k, v in table.items():
        if k in s:
            return v
    return None


def _max_loop_depth(node: ast.AST) -> int:
    best = 0

    def walk(n, depth):
        nonlocal best
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.For, ast.While)):
                best = max(best, depth + 1)
                walk(child, depth + 1)
            else:
                walk(child, depth)

    walk(node, 0)
    return best


def _has_halving(code: str) -> bool:
    return bool(re.search(r"//\s*2|>>\s*1|\bmid\b|/\s*2\b", code))


def _has_recursion(tree: ast.AST) -> bool:
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    for f in funcs:
        for n in ast.walk(f):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == f.name:
                return True
    return False


def estimate_complexity(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    depth = _max_loop_depth(tree)
    halving = _has_halving(code)
    recursive = _has_recursion(tree)

    if depth >= 3:
        return "O(n^3)"
    if depth == 2:
        return "O(n^2)"
    if depth == 1:
        return "O(n log n)" if halving else "O(n)"
    # 无显式循环
    if recursive:
        return None            # 递归复杂度难以静态判定，交由安全边界（不武断）
    if halving:
        return "O(log n)"
    return "O(1)"


def check_complexity(code: str, claimed: str) -> VerifyResult:
    claim = _normalize(claimed)
    est = estimate_complexity(code)
    if claim is None:
        return VerifyResult("complexity", None, f"无法解析声明的复杂度「{claimed}」", confidence=0.3)
    if est is None:
        return VerifyResult("complexity", None,
                            f"代码含递归/结构复杂，静态难判定；声明为 {claim}，建议结合运行或人工确认",
                            confidence=0.3)
    if est == claim:
        return VerifyResult("complexity", True, f"AST 估计为 {est}，与声明 {claim} 一致", confidence=0.7)
    return VerifyResult("complexity", False,
                        f"AST 估计为 {est}，与声明 {claim} 不一致（启发式，供参考）",
                        confidence=0.5, extra={"estimated": est, "claimed": claim})
