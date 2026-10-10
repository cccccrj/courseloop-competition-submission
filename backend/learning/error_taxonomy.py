"""数据结构与算法错因分类体系。

一级错因 + DS&A 二级示例，供 error_diagnoser 约束输出、practice_generator 针对性出题、
错题本聚合统计使用。定位「第一个导致后续偏离的步骤」，避免连锁错误重复计数。
"""
from __future__ import annotations

# code -> (名称, DS&A 典型二级示例, 后续动作)
#
# `follow_up` 是后加的，起因是一次外部评审：一个只能贴标签、贴完没有下一步的错因，
# 对学生和助教都等于没说。加上它之后，"这个标签该怎么处置"成了定义的一部分 ——
# 新增错因时如果写不出 follow_up，那多半说明这个标签本身不成立。
#
# `careless` 是这条规则下最难写的一个，也确实是十个里唯一说不出教学动作的：
# 原先它的示例是"掌握知识但偶发操作失误"，等于给诊断模型开了一个万能垃圾桶 ——
# 凡是归不进前九类的都可以扔进来，而扔进来之后系统什么也做不了。
# 现在给它加了举证门槛（提示词里写明，代码里 enforce_careless_gate 兜底）：只有学生在同一知识点上
# 已经有做对的记录时才允许用它，否则必须落到具体错因上。它的后续动作也不是重讲，
# 而是同型题复测 —— 真是手滑就能过，过不了说明本来就不是手滑。
ERROR_TAXONOMY: dict[str, dict] = {
    "knowledge_gap":   {"name": "知识缺失", "examples": ["不知道某数据结构定义", "不清楚算法适用条件"],
                        "follow_up": "补前置知识后重讲，再出基础题确认"},
    "concept_confuse": {"name": "概念混淆", "examples": ["栈↔队列", "BFS↔DFS", "完全二叉树↔满二叉树", "稳定↔不稳定排序"],
                        "follow_up": "对比讲解两个易混概念，出辨析题"},
    "misread":         {"name": "审题错误", "examples": ["漏条件", "看错目标", "忽略数据规模/取值范围"],
                        "follow_up": "带学生重读题干标出条件，出条件相近的干扰题"},
    "strategy":        {"name": "策略错误", "examples": ["选错数据结构或算法", "该用哈希却线性扫", "该用堆却全排序"],
                        "follow_up": "讲清各方案的适用条件，出选型对比题"},
    "reasoning":       {"name": "推理错误", "examples": ["递归终止条件缺失", "循环不变式错误", "归纳跳步"],
                        "follow_up": "拆解推理链逐步验证，出补全步骤题"},
    "complexity":      {"name": "复杂度分析错误", "examples": ["忽略最坏情况", "摊还分析错误", "漏算递归栈空间"],
                        "follow_up": "重算最坏与摊还情况，出复杂度分析题"},
    "boundary":        {"name": "边界遗漏", "examples": ["空表/单节点", "越界", "重复元素", "自环/负权", "整数溢出"],
                        "follow_up": "补一份边界清单，出极端样例题"},
    "calculation":     {"name": "计算错误", "examples": ["下标偏移", "取模错误", "数值/符号错误"],
                        "follow_up": "不重讲概念，出同型计算题练准确率"},
    "incomplete":      {"name": "表达不完整", "examples": ["结论对但缺步骤或依据"],
                        "follow_up": "要求补全依据，出需要写明理由的题"},
    "careless":        {"name": "粗心失误", "examples": ["同一知识点此前已做对，本次仅抄写/誊写出错"],
                        # 原写"限时复测"，但系统没有限时复测功能 —— follow_up 只写做得到的事
                        "follow_up": "不重讲，出同型题复测；再错则按具体错因重判"},
}

ERROR_CODES = list(ERROR_TAXONOMY)

# 指向"理解岔了"而非"练得不够"的错因。反复出现这两类，说明该重讲而不是多刷题 ——
# 掌握度模型的 misconception 罚分与 misconception 状态由它们触发。
# 定义在这里而不是各调用点各写一份：判题写回与薄弱画像必须用同一个判据。
MISCONCEPTION_TAGS = ("knowledge_gap", "concept_confuse")


# canonical 答案形态里"不是算出来的"那几种。选错项、选错集合，跟算错值是两回事。
_NON_NUMERIC_KINDS = ("choice", "set")


def name_of(code: str) -> str:
    return ERROR_TAXONOMY.get(code, {}).get("name", code)


def follow_up_of(code: str) -> str:
    """这个错因该怎么处置。界面上跟着标签一起显示，让"贴了标签"必然带出"下一步"。"""
    return ERROR_TAXONOMY.get(code, {}).get("follow_up", "")


def error_type_for_canonical(canonical) -> str:
    """确定性判错时，由 canonical 答案形态推错因。

    只区分"选错项"与"算错值"这两类，更细的判定要靠 LLM 诊断，猜不出来就别猜。
    住在这里而不是判题端点里：摸底测试也走确定性判定，两处必须给出同一个标签 ——
    这个标签会被 /practice/generate 当作出题约束，标错了变式题就打偏。
    """
    kind = canonical.get("kind") if isinstance(canonical, dict) else None
    return "knowledge_gap" if kind in _NON_NUMERIC_KINDS else "calculation"


def enforce_careless_gate(tags: list[str], had_correct_before: bool) -> list[str]:
    """careless 的举证门槛，从提示词下沉到代码（docs/35 §2.2）。

    提示词里早就写了"只有该生在同一知识点做对过才允许用 careless"，但模型不一定照做，
    而这一条恰恰没法事后靠人工抽查补救 —— 一旦判成粗心，系统就不再重讲。
    没有做对记录时：换成诊断给出的下一个具体错因；一个都没有就落到 calculation
    （粗心最常见的真实形态就是算错/抄错数值），而不是保留一个撑不住的标签。
    """
    if not tags or tags[0] != "careless" or had_correct_before:
        return list(tags)
    rest = [t for t in tags if t != "careless"]
    return rest or ["calculation"]


def review_reason_for(error_type: str, verification: dict | None = None,
                      has_evidence: bool | None = None) -> str:
    """这条错题为什么值得助教看一眼（docs/35 §2.2 的入队原因）。

    按"AI 结论有多站不住"排：确定性验证（规则/数值/测试）明确没通过 → 没检索到任何
    教材依据 → 用了有举证门槛的 careless → 其余是普通错题。学生主动申请走
    /review-requests，不经过这里。
    """
    if isinstance(verification, dict) and verification.get("passed") is False:
        return "verify_failed"
    if has_evidence is False:
        return "no_evidence"
    if error_type == "careless":
        return "careless"
    return "manual"


def normalize(tag: str) -> str:
    """把自由文本错因标签映射到标准 code；命中名称或 code 均可。"""
    t = (tag or "").strip().lower()
    for code, info in ERROR_TAXONOMY.items():
        if t == code or info["name"] in tag:
            return code
    return tag  # 未识别则原样返回（不丢信息）


def taxonomy_prompt(course=None) -> str:
    """给 LLM 的可选错因清单文本。

    course 给定时（backend.core.courses.Course），用该门的 error_examples 覆盖对应 code 的
    通用二级示例，未覆盖的 code 沿用 ERROR_TAXONOMY 默认示例。course=None 时即数据结构默认。
    """
    overrides = getattr(course, "error_examples", None) or {}
    lines = []
    for code, info in ERROR_TAXONOMY.items():
        examples = overrides.get(code) or info["examples"]
        lines.append(f"- {code}（{info['name']}）：{'、'.join(examples)}")
    return "\n".join(lines)
