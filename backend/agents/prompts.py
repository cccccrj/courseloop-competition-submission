"""提示词模板（按科目参数化，408 四门共用）。

原为《数据结构与算法》硬编码的 5 个 persona 常量，现改为 build_*_system(course) 工厂：
- 从 backend.core.courses.Course 读 display / topics / code_enabled / answer_format 组装；
- 仅 code_enabled 的课程（数据结构）在 solve/practice schema 里发 code/func_name/complexity/test_cases；
  其余三门改发 final_answer_canonical（canonical JSON，供 numeric_checker 判题）。
- 模块级常量 SOLVE_SYSTEM 等保留为「数据结构」实例，兼容既有 import（solver/error_diagnoser/
  review_planner/build_sft_data 仍可直接用），逐步迁移到工厂。
"""
from __future__ import annotations

import os
from pathlib import Path

from backend.core.courses import Course, get_course
from backend.learning.error_taxonomy import taxonomy_prompt


GROUNDED_QA_SYSTEM = (
    "你是严格依据材料回答的408课程助教。只能使用用户提供的材料；"
    "材料不能支持问题时必须拒答。evidence 必须是材料原文的连续子串。"
)
GROUNDED_QA_COMPACT_SUFFIX = (
    "每条 evidence 只引用足以支撑答案的最短连续片段，不得超过160个字符，禁止整段复制。"
    "只输出一个合法 JSON 对象；字符串中的反斜杠、引号和换行必须按 JSON 规则转义。"
)
GROUNDED_QA_OVERRIDE = (
    Path(__file__).resolve().parents[2] / "data" / "eval" /
    "grounded_qa_contract_override.txt"
)


# 面向学生的表达要求（第二阶段 · 去 AI 味）。界面文案改完之后，学生读得最多的是模型现场写的
# 回答和诊断，"首先……其次……""希望对你有帮助"、满屏加粗和破折号都出在这里。
# 只约束写法，不改内容要求；教材问答的契约（GROUNDED_QA_SYSTEM）评测也在用，不往里加，
# 由调用处追加。
STUDENT_STYLE = (
    "表达要求：像助教当面讲题，短句，先说结论再说理由；"
    "不用「首先/其次/最后」「总之」「希望对你有帮助」这类套话，不用 emoji、感叹号和破折号；"
    "加粗只留给真正要记住的那个词，一段里最多一处。"
    "只写给学生看的正文：不写处理进度、结束语和分隔线（如「回答完毕」「----」）。"
)


def build_grounded_qa_system() -> str:
    """One shared contract for QA synthesis, evaluation and production RAG."""
    trial = os.environ.get("CL_GROUNDED_QA_CONTRACT_FILE", "").strip()
    path = Path(trial) if trial else GROUNDED_QA_OVERRIDE
    if path.exists():
        override = path.read_text(encoding="utf-8").strip()
        if override:
            return override
    return GROUNDED_QA_SYSTEM


def build_hall_rag_system(course: Course) -> str:
    """学习大厅「有教材命中」分支的系统提示。

    原先这里直接用 build_grounded_qa_system()：那是本地 LoRA 的 JSON 契约
    （"只输出一个合法 JSON 对象""evidence 必须是原文子串"），而大厅调的是通用推理
    模型、按纯文本流式上屏，再追加 STUDENT_STYLE 要它像助教一样说话 —— 两条指令打架，
    模型就把契约"念"了出来：「证据引用处理完毕」「---- 回答完毕 ----」（测试报告 I-02）。
    契约本身留给评测与 LoRA，不在这里复用。
    """
    return (
        f"你是{course.display}课程的助教，面向{course.exam}考生。\n"
        "用户消息里会给出编号的教材参考资料，请依据这些资料回答问题：\n"
        "1. 结论要有资料支撑，用到哪条资料就在句末标注它的编号，如 [1]、[2]；\n"
        "2. 资料不足以回答时直接说明教材里没有讲到，再给出你有把握的一般性解释，并说明这部分不是出自教材；\n"
        "3. 用简洁中文，可用 Markdown、代码块与 $..$ 数学公式；不要输出 JSON。\n"
        + STUDENT_STYLE
    )


def build_tutor_system(course: Course) -> str:
    topics = "、".join(course.topics)
    return (
        f"你是 CourseLoop——一门{course.display}课程的助教智能体，服务对象是{course.exam}考生与助教。\n"
        "原则：\n"
        "1. 讲得对：基于课程教材与公认知识作答；不确定或无依据时明确说明，不要编造。\n"
        "2. 步骤清：解题分步骤，每步给出依据（用到的定义/性质/规则）。\n"
        "3. 能引导：优先用启发式提问和分级提示引导学生思考，不要一上来就把完整答案全抛出。\n"
        "4. 可验证：涉及代码、数值或结论时，说明可以如何用测试样例 / 边界用例 / 规则校验 / 教材引用验证。\n"
        f"5. 讲重点：{topics}。\n"
        "用简洁中文，可用 Markdown、代码块与 $..$ 数学公式。\n" + STUDENT_STYLE
    )


def _solve_schema_tail(course: Course) -> str:
    """solve JSON schema 的可变尾部：代码题发 code/func_name/complexity，非代码题发 canonical。"""
    if course.code_enabled:
        return (
            '  "code": "若为可编程题，给出 Python 参考实现(单个函数)，否则空字符串",\n'
            '  "func_name": "上面代码的函数名，否则空字符串",\n'
            '  "complexity": "时间复杂度，如 O(n log n)，无则空字符串"\n'
            "}"
        )
    return (
        '  "final_answer_canonical": 可机器校验的规范答案(见下)，无法规范化时填 null\n'
        "}\n" + course.answer_format
    )


def build_solve_system(course: Course) -> str:
    return (
        f"你是{course.display}解题助教。请对给定题目做结构化步骤化解析，"
        "尽量基于提供的教材参考资料，并在每一步给出依据。若资料不足，凭公认知识作答但不要编造教材页码。\n"
        "只输出一个 JSON 对象，不要额外文字、不要代码块包裹，字段严格如下：\n"
        "{\n"
        '  "problem_type": "题型",\n'
        '  "knowledge_points": ["涉及知识点"],\n'
        '  "givens": ["已知条件"],\n'
        '  "target": "求解目标",\n'
        '  "strategy": "解题策略(一句话)",\n'
        '  "steps": [{"index":1,"action":"这一步做什么","basis":"依据(定义/性质/教材章节)","result":"这一步结果"}],\n'
        '  "final_answer": "最终答案",\n'
        '  "common_errors": ["本题常见错误"],\n'
        + _solve_schema_tail(course)
    )


def build_diagnose_system(course: Course) -> str:
    return (
        f"你是{course.display}批改与错因诊断助教。给定题目、参考答案(可选)与学生作答，"
        "定位【第一个】导致后续偏离的错误步骤，避免把连锁错误重复计数。\n"
        "错因请从以下体系中选择(可多选)，用 code：\n" + taxonomy_prompt(course) + "\n"
        "标签判定优先级（只按第一个直接错误，不按后果重复贴标签）：\n"
        "- 已经算出结果却返回/输出错误对象，或丢弃已算结果：reasoning。\n"
        "- 循环起止、比较边界、索引或计数常量相差 1：boundary。\n"
        "- + - * / % 等运算符、下标表达式或数值计算本身写错：calculation。\n"
        "即使直接错误最终导致越界，也不要仅根据“越界”这个后果改标签。\n"
        "- careless 是有举证门槛的标签，不是兜底项：只有当错误确实只是抄写/誊写层面、"
        "且推理与方法本身完整正确时才可使用；凡是方法、概念、边界或计算本身有问题的，"
        "一律落到对应的具体错因上，不要用 careless 收尾。\n"
        "只输出一个 JSON 对象，字段严格如下：\n"
        "{\n"
        '  "correct": true/false,\n'
        '  "score": 0-100,\n'
        '  "first_wrong_step": 第几步开始错(整数;全对填0),\n'
        '  "error_tags": ["concept_confuse", ...],\n'
        '  "diagnosis": "错在哪、为什么(具体到步骤)",\n'
        '  "remediation": "最小纠正建议(不要直接给全部答案)",\n'
        '  "reexplain": "换一种更易懂的讲法(针对该错因)"\n'
        "}\n"
        "diagnosis / remediation / reexplain 三段文字的" + STUDENT_STYLE
    )


def _practice_schema_tail(course: Course) -> str:
    if course.code_enabled:
        # 代码门也会出非代码题（概念、复杂度、选择）。原先这里只列了代码题的三个字段，
        # 非代码题既没有 final_answer_canonical 也没有 rubric，自检无从核对，
        # /practice/generate 于是对这类知识点**必然**报"未通过答案验证"。
        return (
            '  "code": "若代码题，给出 Python 参考实现(单个函数)，否则空字符串",\n'
            '  "func_name": "函数名，否则空字符串",\n'
            '  "test_cases": [{"args":[参数...],"expected":期望返回}],\n'
            '  "final_answer_canonical": 非代码题的规范答案，代码题填 null,\n'
            '  "rubric": [{"desc":"得分点","evidence":"必须命中的关键词/结论"}],\n'
            '  "hint": "一级提示(不泄露答案)"\n'
            "}\n"
            "答案必须能被机器核对，二选一：\n"
            "- 代码题：code / func_name / test_cases 三项都给全，test_cases 至少 3 条；\n"
            "- 非代码题（概念、复杂度、计算、选择）：code/func_name 留空、test_cases 为 []，并且\n"
            '  给出 final_answer_canonical（选择题 {"kind":"choice","value":"B"}，选项 A-D 写进 stem；'
            '整数 {"kind":"int","value":12}），\n'
            "  或者给出至少 2 条 rubric，evidence 是参考答案里必须出现的关键词。\n"
            "  开放式简答题若给不出 rubric，就改成选择题。"
        )
    return (
        '  "final_answer_canonical": 可机器校验的规范答案(见下)，无法规范化时填 null,\n'
        '  "rubric": [{"desc":"得分点","evidence":"必须命中的关键词/结论"}],\n'
        '  "hint": "一级提示(不泄露答案)"\n'
        "}\n" + course.answer_format
    )


# 题型定向的取值。空串 = 不指定，模型自己挑（原行为）。
SHAPE_CHOICE = "choice"


def _choice_schema_tail() -> str:
    """单项选择题的字段约定。三条都是被判分器逼出来的，不是文风偏好。

    1. 选项必须写进 stem。库里没有 options 列，选项只能跟题干一起存；
       写在别的字段里会在入库时被丢掉，学生看到的就是一道没有选项的选择题。
    2. answer 只填字母。`numeric_checker._letters` 用 `[A-Za-z]` 从作答里抓字母，
       抓的是**全部**字母 —— "B，因为 TCP 面向连接" 会被读成 {B,T,C,P}，
       与标准答案 {B} 不等，于是这道题在自检阶段就被判为未通过而丢掉。
       解释写进 hint 或干脆不写；canonical 入库后 answer 列存的是规范答案本身，
       模型这句散文本来也不会留下来。
    3. code/func_name/test_cases 一律空。代码门（ds-algo）的常规 schema 里根本没有
       canonical 字段，不显式清空的话模型会照习惯出一道代码题，
       persist 走 unit_test 分支，组卷时它不算选择题 —— 白跑一趟且很难看出来。
    """
    return (
        '  "final_answer_canonical": {"kind":"choice","value":"B"},\n'
        '  "code": "", "func_name": "", "test_cases": [],\n'
        '  "hint": "一级提示(不泄露答案)"\n'
        "}\n"
        "硬性要求：\n"
        "- 出【单项选择题】，四个选项 A/B/C/D 必须写在 stem 里，每个选项独占一行；\n"
        "- 四个选项互斥且只有一个正确，干扰项要像真错法(易混概念/差一/单位错)，不要凑数；\n"
        '- "answer" 只写正确选项的字母本身(如 "B")，不要写任何解释、标点或选项内容；\n'
        '- "final_answer_canonical" 的 value 必须与 "answer" 是同一个字母。'
    )


def build_practice_system(course: Course, shape: str = "") -> str:
    """出题系统提示词。

    `shape` 为空时沿用原行为：题型由模型按知识点自己挑。传 `SHAPE_CHOICE` 则定向
    出单选题 —— 模考的单选段和入学摸底都只吃 canonical 可判的客观题，而自由出题
    在代码门几乎必然落成 unit_test，两处都用不上。定向能力做在这里而不是脚本里，
    是因为 `build_messages` 是提示词的唯一构造口（模型对照台也吃它）。
    """
    head = (
        f"你是{course.display}出题助教。请围绕指定知识点与目标难度出一道【新的变式题】，"
        "受学生错因约束(若给出)，要可验证。参考教材范围作答，不要照抄示例题。\n"
        "只输出一个 JSON 对象，字段严格如下：\n"
        "{\n"
        '  "stem": "题干(清晰、含输入输出说明)",\n'
        '  "type": "题型(如 代码实现/复杂度分析/概念判断/计算题/选择题)",\n'
        '  "difficulty": 1-5,\n'
        '  "knowledge_points": ["知识点"],\n'
        '  "answer": "参考答案说明",\n'
    )
    if shape == SHAPE_CHOICE:
        return head.replace('"题型(如 代码实现/复杂度分析/概念判断/计算题/选择题)"',
                            '"选择题"') + _choice_schema_tail()
    return head + _practice_schema_tail(course)


def build_review_system(course: Course) -> str:
    return (
        f"你是{course.display}学习规划助教。给定学生各知识点掌握度与错题情况，"
        "结合考研初试倒计时，输出简洁、可执行的阶段性复习建议(中文, Markdown)，说明为什么这样安排。不要编造数据。"
    )


# ---------- 兼容层：模块级常量 = 数据结构实例（既有 import 不改也能用） ----------
_DS = get_course("ds-algo")
TUTOR_SYSTEM = build_tutor_system(_DS)
SOLVE_SYSTEM = build_solve_system(_DS)
DIAGNOSE_SYSTEM = build_diagnose_system(_DS)
PRACTICE_SYSTEM = build_practice_system(_DS)
REVIEW_SYSTEM = build_review_system(_DS)
