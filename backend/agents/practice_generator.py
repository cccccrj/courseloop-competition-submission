"""个性化练习生成 agent：按知识点/错因/难度出变式题，【先验证后展示】。"""
from __future__ import annotations

import json

from backend.core import llm as llm_mod
from backend.core import db
from backend.core import telemetry
from backend.core.courses import get_course
from backend.core.jsonio import extract_json
from backend.agents.prompts import build_practice_system
from backend.learning.error_taxonomy import name_of
from backend.verification.unit_test_runner import run_python_tests
from backend.verification.numeric_checker import check_numeric
from backend.verification.rule_checker import check_concept


def _verify_noncode(data: dict) -> dict | None:
    """非代码题的确定性自检：优先 canonical 数值自洽，其次 rubric 得分点覆盖。判不了返回 None。"""
    canonical = data.get("final_answer_canonical")
    if canonical:
        return check_numeric(str(data.get("answer", "")), canonical).to_dict()
    rubric = data.get("rubric") or []
    must_include = [r.get("evidence") or r.get("desc") for r in rubric if (r.get("evidence") or r.get("desc"))]
    if must_include:
        return check_concept(str(data.get("answer", "")), must_include=must_include).to_dict()
    return None


def build_user_prompt(knowledge_point: str, context: str = "", error_type: str = "",
                      difficulty: int = 2, choice_letter: str = "",
                      avoid: list[str] | None = None, facet: str = "") -> str:
    """本题的具体约束。`choice_letter` 指定正确选项落在哪个字母。

    `avoid` 是该知识点已出过的题干。不传的话，同一个知识点连出三道会得到**同一道题
    换选项顺序**：实测「最短路径」连出的三道题干相似度 0.97 / 0.93 / 0.90，其中
    一道还把答案写进了题干（"A到B的最短路径长度为5…请问A到B的最短路径长度是多少"）。
    入库去重按题干精确匹配，这种近似重复一道都拦不住 —— 只能在出题时就避开。

    加它是因为 schema 里的示例写的是 `{"kind":"choice","value":"B"}`，模型会照着抄：
    实测连出三道题、正确答案全是 B。一份 40 道单选全选 B 的卷子，学生蒙都能蒙对，
    而且演示时一眼就看出来。指定字母比在提示词里写"请随机分布"可靠 ——
    后者要靠模型自觉，前者由调用方轮转，结果可复现。
    题干由模型自己写，把正确内容放到指定字母那一项即可，不影响题目质量。
    """
    constraint = [f"目标知识点：{knowledge_point}", f"目标难度：{difficulty}（1易~5难）"]
    if facet:
        constraint.append(f"本题考查角度：{facet}（必须从这个角度出，不要换成别的角度）")
    if error_type:
        constraint.append(f"学生近期错因：{name_of(error_type)}（请针对此错因设计）")
    if choice_letter:
        constraint.append(f"本题正确选项必须是 {choice_letter}："
                          f"把正确的那一项写在 {choice_letter} 上，"
                          f'"answer" 与 final_answer_canonical.value 都填 "{choice_letter}"。')
    if avoid:
        listed = "\n".join(f"- {t[:80]}" for t in avoid[-6:])
        constraint.append(
            "本知识点已出过下列题，**不得重复、也不得只改选项顺序或换个变量名**，"
            f"请换一个考查角度：\n{listed}")
    if context:
        constraint.append(f"教材范围参考：\n{context}")
    return "\n".join(constraint) + "\n\n请出一道新的变式题："


def build_messages(knowledge: str, difficulty: int = 2, course: str = "ds-algo",
                   context: str = "", error_type: str = "", shape: str = "",
                   choice_letter: str = "", avoid: list[str] | None = None,
                   facet: str = "") -> list[dict]:
    """生产提示词的唯一构造口。

    当初是为模型对照台抽出来的（那页要让两侧模型吃同一份提示词）。对照台已经删了，
    但这个口子留着：出题这条链路上任何一处要用提示词，都从这里拿，不各自再拼一份 ——
    拼第二份就等于两处早晚不一致，而出题质量的归因本来就难，多一个变量更难。

    `shape` 默认空 = 原行为。
    """
    return [{"role": "system", "content": build_practice_system(get_course(course), shape)},
            {"role": "user", "content": build_user_prompt(knowledge, context, error_type,
                                                          difficulty, choice_letter, avoid,
                                                          facet)}]


def generate(knowledge_point: str, context: str = "", error_type: str = "",
             difficulty: int = 2, max_retries: int = 1, course: str = "ds-algo",
             shape: str = "", choice_letter: str = "",
             avoid: list[str] | None = None, facet: str = "",
             require_verified: bool = False) -> dict:
    """生成一道变式题。代码题用参考答案自跑测试；非代码题用 canonical/rubric 自检。verified=True 才算过。

    `require_verified=True`（个性化练习用）时，"无法核对"也要重出：那条链路只展示并入库
    已验证的题，拿一道核对不了的题回去只会让接口报错。默认 False 保持原行为
    （解题页的变式题允许"待验证"展示）。

    `shape=SHAPE_CHOICE` 定向出单选题（模考单选段与入学摸底只吃这种）；默认空为原行为。
    """
    result: dict = {}
    for kind, payload in generate_events(knowledge_point, context, error_type, difficulty,
                                         max_retries, course, shape, choice_letter, avoid,
                                         facet, require_verified, stream=False):
        if kind == "result":
            result = payload
    return result


# 重出的原因 → 追加给模型的说明。键同时作为事件里的 reason 发给前端，页面据此写"为什么重出"。
_RETRY_NOTES = {
    "parse": "（上一次的输出不是合法 JSON，请严格按字段输出）",
    "tests_failed": "（上一次生成的参考答案未通过自测，请修正题目/答案/样例后重新出题）",
    "unverifiable": ("（上一次出的题答案无法机器核对：请改成带 final_answer_canonical 的"
                     "选择题/数值题，或给出至少 2 条 rubric 关键词，再重新出题）"),
    "mismatch": "（上一次生成的参考答案与规范答案不一致，请修正题目/答案后重新出题）",
}


def generate_events(knowledge_point: str, context: str = "", error_type: str = "",
                    difficulty: int = 2, max_retries: int = 1, course: str = "ds-algo",
                    shape: str = "", choice_letter: str = "",
                    avoid: list[str] | None = None, facet: str = "",
                    require_verified: bool = False, *, stream: bool = False):
    """出题的逐步过程，按顺序产出 (kind, payload)：

      ("attempt", n)          第 n 次出题开始
      ("token", 文本增量)      仅 stream=True：模型正在写的内容
      ("verify", None)        出完了，开始核对答案
      ("retry", 原因)          这次不行，要重出（parse / tests_failed / unverifiable / mismatch）
      ("result", data)        最终结果（与 generate() 的返回值相同）

    个性化练习页原先干等 5–20 秒、只看到一个转圈（docs/36 §6 之后用户反馈"有点慢"）。
    gpt-4o-mini 没有可展示的推理过程，能如实展示的就是这些真实步骤和正在写出来的题目。
    generate() 与流式接口共用这一份逻辑，不各写一遍。
    """
    course_obj = get_course(course)
    user = build_user_prompt(knowledge_point, context, error_type, difficulty,
                             choice_letter, avoid, facet)

    last: dict = {}
    for attempt in range(max_retries + 1):
        yield ("attempt", attempt + 1)
        msgs = [{"role": "system", "content": build_practice_system(course_obj, shape)},
                {"role": "user", "content": user}]
        if stream:
            parts: list[str] = []
            for delta in llm_mod.ask_task_stream("practice", msgs):
                parts.append(delta)
                yield ("token", delta)
            raw = "".join(parts)
        else:
            raw = llm_mod.ask_practice(msgs)
        yield ("verify", None)
        data = extract_json(raw)
        if not data:
            telemetry.log_llm_call("practice", msgs, raw, parsed=None)
            last = {"error": "生成结果解析失败", "raw": raw[:500]}
            if attempt < max_retries:
                yield ("retry", "parse")
            user += "\n\n" + _RETRY_NOTES["parse"]
            continue

        code = (data.get("code") or "").strip()
        func = (data.get("func_name") or "").strip()
        tests = data.get("test_cases") or []
        if code and func and tests:
            vr = run_python_tests(code, func, tests)
            data["verification"] = vr.to_dict()
            data["verified"] = bool(vr.passed)
            telemetry.log_llm_call("practice", msgs, raw, parsed=data, verification=data["verification"])
            if vr.passed:
                yield ("result", data)     # 先验证后展示：通过即返回
                return
            last = data
            if attempt < max_retries:
                yield ("retry", "tests_failed")
            user += "\n\n" + _RETRY_NOTES["tests_failed"]
            continue
        # 非代码题：canonical 数值自洽 / rubric 得分点覆盖做确定性自检
        verification = _verify_noncode(data)
        data["verification"] = verification
        passed = verification.get("passed") if verification else None
        data["verified"] = passed if passed is None else bool(passed)
        telemetry.log_llm_call("practice", msgs, raw, parsed=data, verification=verification)
        if passed is True or (passed is None and not require_verified):
            yield ("result", data)         # True(已验证)；不强求验证时 None(无法判定)也展示
            return
        last = data
        reason = "unverifiable" if passed is None else "mismatch"
        if attempt < max_retries:
            yield ("retry", reason)
        user += "\n\n" + _RETRY_NOTES[reason]
    # 多次仍未通过：返回最后一版。verified 保留原值 —— None 是"核对不了"，False 是
    # "核对了但不对"，调用方要按这两种情况分别措辞，不能都说成"未通过验证"。
    last.setdefault("verified", False)
    yield ("result", last)


_CITE_FIELDS = ("source", "title_path", "page_start", "page_end", "doc_type")


def _citation_payload(citations: list[dict] | None) -> str:
    """把检索命中收敛成入库用的出处串。没有出处就存空串，不存 "[]"。

    只留定位字段（出处/标题路径/页码/类别），丢掉 excerpt 与 score ——
    正文在索引里，抄一份进题库只会让两处慢慢不一致；score 是那次检索的排名分，
    换个查询就变了，对"这题依据哪一页"没有意义。
    """
    if not citations:
        return ""
    out = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        item = {k: c[k] for k in _CITE_FIELDS if c.get(k)}
        if item.get("source"):
            out.append(item)
    return json.dumps(out, ensure_ascii=False) if out else ""


def persist(data: dict, course: str, knowledge: str, source: str = "generated",
            citations: list[dict] | None = None, difficulty: int | None = None) -> int:
    """把通过确定性验证的生成题幂等写回题库；不可靠的题绝不入库。

    `citations` 是出题所依据的教材片段（`rag_context` 的第二个返回值）。出题时本来
    就检索了教材喂进提示词，但出处一直被丢掉 —— 于是讲评时说不出这题依据哪一页，
    2646 块语料和题库之间没有任何连接。只存 source/title_path/页码这几个定位字段，
    不存正文：正文在索引里，抄一份进题库只会让两处慢慢不一致。
    """
    if data.get("verified") is not True:
        return 0
    stem = (data.get("stem") or "").strip()
    if not stem:
        return 0
    existing = db.problem_id_by_stem(course, stem)
    if existing:
        return existing

    # 难度以**调用方要的**为准，模型自报的只作兜底。提示词里写了"目标难度 N"，
    # 但模型实测每次都回 1 —— 30 道题 30 个 1，那个字段不携带任何信息。
    # 而难度是练习推荐、间隔复习、组卷分层共同依赖的量，全填 1 等于这三处都失准。
    want = difficulty if difficulty is not None else data.get("difficulty", 2)
    difficulty = max(1, min(5, int(want or 2)))
    ptype = str(data.get("type") or "个性化练习")
    code = (data.get("code") or "").strip()
    func = (data.get("func_name") or "").strip()
    tests = data.get("test_cases") or []
    canonical = data.get("final_answer_canonical")
    rubric = data.get("rubric") or []
    # 出题时本来就要求了一级提示（build_practice_system 的 schema 里有 hint 字段），
    # 原先入库时丢掉，题库做题就没有提示可点。存下来它才能真正被用上。
    hint = str(data.get("hint") or "").strip()
    cites = _citation_payload(citations)

    if code and func and tests:
        pid = db.insert_problem(course, ptype, stem, difficulty, str(data.get("answer") or ""),
                                "unit_test", knowledge=knowledge, func_name=func,
                                ref_code=code, source=source, hint=hint, citations=cites)
        for t in tests:
            if "args" in t and "expected" in t:
                db.add_test_case(pid, json.dumps(t["args"], ensure_ascii=False),
                                 json.dumps(t["expected"], ensure_ascii=False),
                                 1 if t.get("hidden") else 0)
    elif canonical:
        pid = db.insert_problem(course, ptype, stem, difficulty,
                                json.dumps(canonical, ensure_ascii=False), "numeric",
                                knowledge=knowledge, source=source, hint=hint, citations=cites)
    elif rubric:
        pid = db.insert_problem(course, ptype, stem, difficulty, str(data.get("answer") or ""),
                                "rule", knowledge=knowledge, source=source, hint=hint,
                                citations=cites)
        for item in rubric:
            db.add_rubric_item(pid, str(item.get("desc") or ""),
                               float(item.get("score", 0) or 0),
                               str(item.get("evidence") or ""))
    else:
        return 0

    kid = db.get_or_create_knowledge_id(knowledge, course)
    db.link_problem_knowledge(pid, kid)
    return pid
