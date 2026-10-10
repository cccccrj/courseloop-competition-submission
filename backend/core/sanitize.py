"""模型输出上屏前的清洗。

模型偶尔会把"流程状态"当正文写出来：「---- 回答完毕 ----」「证据引用处理完毕」、
没剥干净的 <think> 块、<|im_end|> 这类特殊 token。学生看到这些，等于看到了
后端的提示词和解析协议（测试报告 I-02）。这里集中处理，前端
`frontend/src/utils/sanitize.ts` 是同一套规则的兜底副本，改一边记得改另一边。

两层：
- ``strip_markers``：只删整行的控制标记、特殊 token 和思考块，不碰其余内容。
  对 JSON 输出也安全，所以 llm.chat() 的所有返回都过一遍。
- ``clean_answer`` / ``clean_reasoning``：面向学生的散文回答 / 思考过程，在上面
  基础上再做针对性的处理。

刻意不碰正文中间的分隔线和表格：题库里有大量 ``|----|`` 表格和
「所有边处理完毕均未发现环」这类正常句子，规则只认**整行**都是标记的情况。
"""
from __future__ import annotations

import json
import re

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_ORPHAN_THINK_END = re.compile(r"^.*?</think>", re.S | re.I)
_THINK_TAG = re.compile(r"</?think>", re.I)
_SPECIAL_TOKEN = re.compile(r"<\|[^|<>\n]{1,40}\|>")
_WRAPPER_TAG = re.compile(r"</?(?:answer|final_answer|final|response|output)>", re.I)

# 行首尾可能包着的装饰：分隔线、括号、冒号、句号
_DECOR = " \t-=*_—–~·.。:：!！【】[]()（）<>《》#"
_MARKER_LINE = re.compile(
    r"^(?:以上|本次|本轮)?"
    r"(?:回答|作答|输出|答案|证据|证据引用|引用|检索|处理|解析|思考|推理|生成|分析)"
    r"(?:引用|过程|内容|部分|结果)?(?:处理)?(?:已)?"
    r"(?:完毕|结束|完成|开始)$"
)
_EN_MARKER_LINE = re.compile(r"^(?:END|DONE|EOF|END OF ANSWER|ANSWER END)$", re.I)
_RULE_LINE = re.compile(r"^\s*(?:[-=*_—–~]\s*){3,}$")
_FENCE = re.compile(r"^\s*(```|~~~)")

# 思考过程里复述提示词契约的行（clean_reasoning 专用）
_PROMPT_ECHO = re.compile(
    r"evidence|source_id|JSON\s*对象|表达要求|系统提示|提示词|system prompt|STUDENT_STYLE",
    re.I,
)


def _is_marker_line(line: str) -> bool:
    core = line.strip(_DECOR)
    if not core or len(core) > 16:
        return False
    return bool(_MARKER_LINE.match(core) or _EN_MARKER_LINE.match(core))


def _filter_lines(text: str, drop) -> str:
    """逐行过滤，跳过代码块内部（代码里的 --- 和 END 是内容）。"""
    out, in_code = [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
            out.append(line)
            continue
        if not in_code and drop(line):
            continue
        out.append(line)
    return "\n".join(out)


def _trim_edges(text: str) -> str:
    """去掉首尾的空行和纯分隔线（标记被删后常留下孤零零的 ----）。"""
    lines = text.split("\n")
    while lines and (not lines[0].strip() or _RULE_LINE.match(lines[0])):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _RULE_LINE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines)


def strip_markers(text: str | None) -> str:
    """删掉控制标记、特殊 token、思考块。对 JSON 输出也安全。"""
    if not text:
        return ""
    t = _THINK_BLOCK.sub("", text)
    if re.search(r"</think>", t, re.I):
        t = _ORPHAN_THINK_END.sub("", t, count=1)
    t = _THINK_TAG.sub("", t)
    t = _SPECIAL_TOKEN.sub("", t)
    t = _WRAPPER_TAG.sub("", t)
    t = _filter_lines(t, _is_marker_line)
    return _trim_edges(t)


def _unwrap_json_answer(text: str) -> str:
    """整段是 {"answer": ..., "evidence": ...} 时只取 answer。"""
    s = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", s, re.S | re.I)
    if fenced:
        s = fenced.group(1)
    if not (s.startswith("{") and s.endswith("}")):
        return text
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(parsed, dict):
        return text
    if parsed.get("abstain") is True:
        return "教材中没有找到能回答这个问题的内容。"
    answer = parsed.get("answer")
    return str(answer).strip() if isinstance(answer, str) and answer.strip() else text


def clean_answer(text: str | None) -> str:
    """面向学生的散文回答。"""
    return _trim_edges(_unwrap_json_answer(strip_markers(text)))


def clean_reasoning(text: str | None) -> str:
    """面向学生展示的思考过程：再删掉复述提示词契约的行。"""
    t = strip_markers(text)
    t = _filter_lines(t, lambda line: bool(_PROMPT_ECHO.search(line)))
    return _trim_edges(t)
