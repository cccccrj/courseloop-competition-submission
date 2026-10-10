"""步骤化解析 agent：题目 -> 结构化解析(每步带依据) + 确定性验证。

拆成 build_messages + finalize，供非流式与流式复用。
"""
from __future__ import annotations

from backend.core import llm as llm_mod
from backend.core import telemetry
from backend.core.courses import get_course
from backend.core.jsonio import extract_json
from backend.agents.prompts import build_solve_system
from backend.verification.unit_test_runner import run_python_tests
from backend.verification.complexity_checker import check_complexity


def build_messages(problem: str, context: str = "", course: str = "ds-algo") -> list[dict]:
    user = (f"教材参考资料：\n{context}\n\n题目：{problem}\n\n请按要求解析：" if context
            else f"题目：{problem}\n\n请按要求解析：")
    system = build_solve_system(get_course(course))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def finalize(content: str, self_test_cases: list[dict] | None = None,
             messages: list[dict] | None = None, model: str | None = None,
             course: str = "ds-algo") -> dict:
    data = extract_json(content)
    if not data:
        telemetry.log_llm_call("solve", messages or [], content, parsed=None, model=model)
        return {"error": "解析结果解析失败", "raw": content[:500]}
    code = (data.get("code") or "").strip()
    func = (data.get("func_name") or "").strip()
    complexity = (data.get("complexity") or "").strip()
    verification = None
    if code and func and self_test_cases:
        verification = run_python_tests(code, func, self_test_cases).to_dict()
    elif code and complexity:
        verification = check_complexity(code, complexity).to_dict()
    elif not get_course(course).code_enabled and data.get("final_answer_canonical"):
        # 非代码门：用 canonical 答案做自洽数值校验（final_answer 应与 canonical 一致）
        from backend.verification.numeric_checker import check_numeric
        verification = check_numeric(str(data.get("final_answer", "")),
                                     data["final_answer_canonical"]).to_dict()
    data["verification"] = verification
    ov = verification.get("passed") if verification else None
    for s in data.get("steps", []):
        s.setdefault("verified", ov)
    telemetry.log_llm_call("solve", messages or [], content, parsed=data, verification=verification, model=model)
    return data


def solve(problem: str, context: str = "", self_test_cases: list[dict] | None = None,
          course: str = "ds-algo") -> dict:
    msgs = build_messages(problem, context, course=course)
    raw = llm_mod.ask_solve(msgs)
    return finalize(raw, self_test_cases, messages=msgs, course=course)
