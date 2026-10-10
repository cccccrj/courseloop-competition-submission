"""界面文案不许出现写给部署者的东西。

3d70713 清过一轮（脚本名、接口路径），docs/34 §2.2 又核出拍照批改页和学情周报页
仍在教学生去 `.env` 里填 `CL_LLM_*`。这类文案每次都是"写的时候觉得是在帮用户"，
没有断言就只能等评审截图发现。前端没有测试基础设施，所以由后端用例读源码来守
（与 test_frontend_constants.py 同一做法）。

只扫**去掉注释之后**的源码：注释里写配置名是给开发者看的，正常。
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"

FORBIDDEN = [
    (re.compile(r"CL_[A-Z]{2,}"), "环境变量名"),
    (re.compile(r"(?<![\w@/])\.env\b"), ".env 文件"),
    (re.compile(r"\b[\w/]+\.py\b"), "Python 脚本名"),
    (re.compile(r"流式请求失败 \["), "接口路径"),
]

_COMMENT = re.compile(r"<!--.*?-->|/\*.*?\*/|(?<![:'\"`])//[^\n]*", re.S)


def _strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def test_ui_source_has_no_deployment_details():
    offenders: list[str] = []
    for path in sorted([*SRC.rglob("*.vue"), *SRC.rglob("*.ts")]):
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for pattern, what in FORBIDDEN:
            for m in pattern.finditer(code):
                offenders.append(f"{path.relative_to(SRC)}: {what} → {m.group(0)}")
    assert not offenders, "界面文案里出现了部署细节：\n" + "\n".join(offenders)


def test_templates_have_no_literal_markdown_bold():
    """模板里写死的 **加粗** 不会被渲染，页面上直接露两对星号（docs/34 §2.7）。"""
    offenders = []
    for path in sorted(SRC.rglob("*.vue")):
        text = path.read_text(encoding="utf-8")
        tpl = re.search(r"<template>(.*)</template>", text, re.S)
        if not tpl:
            continue
        body = re.sub(r"<!--.*?-->", "", tpl.group(1), flags=re.S)
        body = re.sub(r"\{\{.*?\}\}", "", body, flags=re.S)
        if re.search(r"\*\*[^*\s][^*]*\*\*", body):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders
