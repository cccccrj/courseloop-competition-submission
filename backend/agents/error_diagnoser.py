"""错因诊断 agent：学生作答 -> 首错定位 + 错因标签 + 纠正建议 + 换讲法。

拆成 build_messages + finalize，供非流式(ask_workhorse)与流式(ask_reasoner_stream)复用。
"""
from __future__ import annotations

from backend.core import llm as llm_mod
from backend.core import telemetry
from backend.core.courses import get_course
from backend.core.jsonio import extract_json
from backend.agents.prompts import build_diagnose_system
from backend.learning.error_taxonomy import normalize, name_of
from backend.verification.unit_test_runner import run_python_tests


def build_messages(problem: str, student_answer: str, reference: str = "",
                   student_code: str = "", func_name: str = "",
                   tests: list[dict] | None = None,
                   course: str = "ds-algo", context: str = "") -> tuple[list[dict], dict | None]:
    """构造诊断消息 + 确定性证据（若给学生代码+样例则先跑沙箱）。

    ``context`` 是检索到的教材片段（调用方从 rag_context 拿）。给了就让模型据此讲错因 ——
    诊断此前是四类意图里唯一不接教材的，说得出"你错了"，说不出"教材哪一页这么写的"。
    """
    verification = None
    evidence = ""
    if student_code and func_name and tests:
        vr = run_python_tests(student_code, func_name, tests)
        # 带上 tech：tutor_graph 会把它存进错题的 tech_detail，并在返回页面前去掉
        verification = vr.to_dict(include_tech=True)
        evidence = f"\n（沙箱测试结果：{'通过' if vr.passed else '未通过'} — {vr.details}）"

    parts = []
    if context:
        parts.append(f"教材参考资料：\n{context}")
    parts.append(f"题目：{problem}")
    if reference:
        parts.append(f"参考答案：{reference}")
    parts.append(f"学生作答：{student_answer or student_code}{evidence}")
    parts.append("请定位第一个错误步骤并诊断错因"
                 + ("，并在 diagnosis / remediation 中引用上面的教材依据：" if context else "："))
    messages = [{"role": "system", "content": build_diagnose_system(get_course(course))},
                {"role": "user", "content": "\n\n".join(parts)}]
    return messages, verification


def finalize(content: str, verification: dict | None,
             messages: list[dict] | None = None, model: str | None = None) -> dict:
    data = extract_json(content)
    if not data:
        telemetry.log_llm_call("diagnose", messages or [], content, parsed=None,
                               verification=verification, model=model)
        return {"error": "诊断结果解析失败", "raw": content[:500], "verification": verification}
    tags = [normalize(t) for t in (data.get("error_tags") or [])]
    data["error_tags"] = tags
    data["error_tag_names"] = [name_of(t) for t in tags]
    if verification is not None and verification.get("passed") is not None:
        data["verification"] = verification
        if verification["passed"] and not data.get("correct"):
            data["_note"] = "沙箱测试全部通过，但 LLM 判定有误——以沙箱为准"
    else:
        data.setdefault("verification", verification)
    telemetry.log_llm_call("diagnose", messages or [], content, parsed=data,
                           verification=verification, model=model)
    return data


def diagnose(problem: str, student_answer: str, reference: str = "",
             student_code: str = "", func_name: str = "",
             tests: list[dict] | None = None, course: str = "ds-algo",
             context: str = "") -> dict:
    messages, verification = build_messages(problem, student_answer, reference,
                                            student_code, func_name, tests, course=course,
                                            context=context)
    raw = llm_mod.ask_diagnose(messages)
    return finalize(raw, verification, messages=messages)
