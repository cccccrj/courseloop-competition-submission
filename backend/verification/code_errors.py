"""代码题运行失败 → 给人看的中文说明（测试报告 I-05）。

沙箱只回报"哪类失败 + 异常类名 + 原始信息 + 行号"，由这里翻译成一句中文。
原始的 `SyntaxError: invalid syntax (<string>, line 1)` 只进 tech 字段和日志：
学生和助教在"AI 判定"里看到的是"代码有语法错误（第 1 行），无法运行"，
助教需要时可以在复核卡片的"技术详情"里展开原文。

`rewrite_legacy` 用来改写修复前就存进库里的老文本（scripts/clean_dialog_markers.py）。
"""
from __future__ import annotations

import re

# 失败类别（错误码）。runner 的 VerifyResult.extra["code"] 取这些值。
SYNTAX = "syntax_error"
LOAD = "load_error"
FUNC_MISSING = "func_missing"
RUNTIME = "runtime_error"
TIMEOUT = "timeout"
KILLED = "killed"
SANDBOX = "sandbox_error"

SANDBOX_TEXT = "这次没能运行你的代码，请稍后再试"
KILLED_TEXT = "运行超时或占用内存过多，可能有死循环或复杂度过高"

_SYNTAX_EXC = {"SyntaxError", "IndentationError", "TabError"}


def explain_exception(exc: str, msg: str = "") -> str:
    """异常类名 + 原始信息 → 一句中文（不含英文类名）。"""
    msg = msg or ""
    if exc in _SYNTAX_EXC:
        return "缩进不正确" if exc != "SyntaxError" else "语法错误"
    if exc == "UnboundLocalError":
        m = re.search(r"'([^']+)'", msg)
        return f"变量 `{m.group(1)}` 在赋值之前就被使用了" if m else "有变量在赋值之前就被使用了"
    if exc == "NameError":
        m = re.search(r"name '([^']+)' is not defined", msg)
        return f"用到了没有定义的名字 `{m.group(1)}`" if m else "用到了没有定义的名字"
    if exc == "KeyError":
        return f"字典里没有键 {msg}" if msg else "字典里没有要找的键"
    table = {
        "ZeroDivisionError": "出现了除数为 0 的运算",
        "IndexError": "下标越界（访问了不存在的位置）",
        "TypeError": "类型不匹配（例如参数个数不对，或把不同类型的值放在一起运算）",
        "AttributeError": "对象没有这个属性或方法",
        "ValueError": "传入的值不合法",
        "RecursionError": "递归层数太深，可能缺少终止条件",
        "MemoryError": "占用内存过多",
        "OverflowError": "数值溢出",
        "ImportError": "导入了运行环境里没有的模块",
        "ModuleNotFoundError": "导入了运行环境里没有的模块",
        "AssertionError": "断言没有通过",
        "StopIteration": "迭代器提前结束了",
    }
    return table.get(exc, "运行时出错")


def load_failure_text(exc: str, msg: str = "", line: int | None = None) -> str:
    where = f"（第 {line} 行）" if line else ""
    if exc in _SYNTAX_EXC:
        return f"代码有{explain_exception(exc, msg)}{where}，无法运行"
    return f"代码在加载时出错{where}：{explain_exception(exc, msg)}，无法运行"


def case_failure_text(index: int, exc: str, msg: str = "") -> str:
    return f"样例 {index} 运行时出错：{explain_exception(exc, msg)}"


def func_missing_text(func: str) -> str:
    return f"没有找到函数 `{func}`，请检查函数名是否和题目要求一致"


# ---------- 改写老数据 ----------
_LEGACY_LOAD = re.compile(
    r"代码无法加载: (\w+): (.*?)(?: \(<?[^,()]*>?, line (\d+)\))?$", re.S)
_LEGACY_CASE = re.compile(r"样例 (\d+) 未通过：(\w+(?:Error|Exception|Iteration)): (.*)$", re.S)
_LEGACY_KILLED = re.compile(r"运行被终止（超时或超内存限制，signal \d+）")
_LEGACY_SANDBOX = re.compile(r"沙箱(?:无输出|输出解析失败)：.*$", re.S)
_LEGACY_FUNC = re.compile(r"未找到函数 (\S+)$")


def rewrite_legacy(text: str) -> tuple[str, str]:
    """把修复前存下的原始报错改写成中文。返回 (新文本, 原始技术细节)；不认识就原样返回。"""
    if not text:
        return text, ""
    t = text.strip()
    m = _LEGACY_LOAD.search(t)
    if m:
        line = int(m.group(3)) if m.group(3) else None
        return t[:m.start()] + load_failure_text(m.group(1), m.group(2), line), m.group(0)
    m = _LEGACY_CASE.search(t)
    if m:
        return (t[:m.start()] + case_failure_text(int(m.group(1)), m.group(2), m.group(3)),
                f"{m.group(2)}: {m.group(3)}")
    m = _LEGACY_KILLED.search(t)
    if m:
        return t[:m.start()] + KILLED_TEXT + t[m.end():], m.group(0)
    m = _LEGACY_SANDBOX.search(t)
    if m:
        return t[:m.start()] + SANDBOX_TEXT, m.group(0)
    m = _LEGACY_FUNC.search(t)
    if m:
        return t[:m.start()] + func_missing_text(m.group(1)), m.group(0)
    return text, ""
