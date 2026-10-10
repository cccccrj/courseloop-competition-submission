"""代码题运行失败的说明（测试报告 I-05：复核卡片上直接显示
"代码无法加载: SyntaxError: invalid syntax (, line 1)"）。

纪律：给人看的 details / diagnosis 只有中文、不出现英文异常类名；原始报错只进
extra["tech"] → 错题的 tech_detail 列，且只有助教侧接口下发。
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token
from backend.verification.code_errors import rewrite_legacy
from backend.verification.unit_test_runner import run_python_tests

EXC_NAME = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b|Traceback|signal \d|<string>")
CASES = [{"args": [1], "expected": 2}, {"args": [0], "expected": 1}]


@pytest.mark.parametrize("code, expect", [
    ("def f(x)\n    return x + 1", "代码有语法错误（第 1 行），无法运行"),
    ("def f(x):\n  return 1\n    return 2", "代码有缩进不正确（第 3 行），无法运行"),
    ("def g(x):\n    return x", "没有找到函数 `f`"),
    ("import no_such_module\ndef f(x):\n    return x", "导入了运行环境里没有的模块"),
    ("def f(x):\n    return 1 / x + 1", "样例 2 运行时出错：出现了除数为 0 的运算"),
    ("def f(x):\n    return [][x]", "下标越界"),
    ("def f(x):\n    return y", "用到了没有定义的名字 `y`"),
])
def test_runner_failures_are_explained_in_chinese(code, expect):
    vr = run_python_tests(code, "f", CASES, time_limit=3)
    assert vr.passed is False
    assert expect in vr.details
    assert not EXC_NAME.search(vr.details), vr.details
    # 原始报错还在，只是放到了 tech 里；默认序列化（会返回给页面的那份）不带它
    assert vr.extra.get("tech")
    assert "tech" not in (vr.to_dict().get("extra") or {})
    assert vr.to_dict(include_tech=True)["extra"]["tech"] == vr.extra["tech"]


def test_wrong_answer_still_shows_got_and_expected():
    vr = run_python_tests("def f(x):\n    return x", "f", CASES, time_limit=3)
    assert vr.passed is False and "得到 1，期望 2" in vr.details


@pytest.mark.parametrize("old, new", [
    ("两数之和 || 代码无法加载: SyntaxError: invalid syntax (<string>, line 1)",
     "两数之和 || 代码有语法错误（第 1 行），无法运行"),
    # 报告截图里的样子：<string> 已被 Markdown 吞掉
    ("代码无法加载: SyntaxError: invalid syntax (, line 1)", "代码有语法错误（第 1 行），无法运行"),
    ("题 || 2/5 通过；样例 3 未通过：ZeroDivisionError: division by zero",
     "题 || 2/5 通过；样例 3 运行时出错：出现了除数为 0 的运算"),
    ("如果所有边处理完毕均未发现环", "如果所有边处理完毕均未发现环"),
])
def test_legacy_rows_are_rewritten(old, new):
    assert rewrite_legacy(old)[0] == new


# ---------- 端到端：题库判题 → 错题 → 学生 / 助教两侧 ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    return TestClient(app)


def test_syntax_error_reaches_ta_only_as_tech_detail(client):
    db.register_user("code_stu", "pw123456", name="写代码的", role="student")
    pid = db.insert_problem("ds-algo", "编程题", "实现 f(x) = x + 1", 2, "return x + 1",
                            "unit_test", knowledge="线性表", func_name="f")
    db.add_test_case(pid, json.dumps([1]), json.dumps(2))
    stu = {"Authorization": f"Bearer {issue_token('code_stu', 'student')}"}
    ta = {"Authorization": f"Bearer {issue_token('ta01', 'ta')}"}

    r = client.post("/problems/judge", headers=stu, json={
        "problem_id": pid, "code": "def f(x)\n    return x + 1", "func_name": "f",
        "course": "ds-algo"}).json()
    assert r["passed"] is False and "代码有语法错误" in r["details"]
    assert "_tech" not in r and not EXC_NAME.search(json.dumps(r, ensure_ascii=False))

    mine = client.get("/records/errors?course=ds-algo", headers=stu).json()["items"]
    assert "代码有语法错误（第 1 行）" in mine[0]["diagnosis"]
    assert "tech_detail" not in mine[0]
    assert not EXC_NAME.search(json.dumps(mine, ensure_ascii=False))

    queue = client.get("/teaching/errors?course=ds-algo", headers=ta).json()["items"]
    row = next(x for x in queue if x["username"] == "code_stu")
    assert not EXC_NAME.search(row["diagnosis"])
    assert row["tech_detail"].startswith("SyntaxError")


# ---------- 其他页面：业务 ValueError 的透传 ----------

def test_user_detail_passes_chinese_and_hides_python_errors():
    from backend.api.core_routes import _user_detail
    assert _user_detail(ValueError("模考场次不存在")) == "模考场次不存在"
    # Python 自己抛的英文 ValueError、以及 JSONDecodeError（ValueError 子类）都不外发
    hidden = _user_detail(ValueError("Invalid isoformat string: 'abc'"))
    assert "isoformat" not in hidden and "请检查" in hidden
    try:
        json.loads("{bad")
    except ValueError as exc:
        assert "Expecting" not in _user_detail(exc)


def test_no_route_forwards_raw_exception_text():
    """防回归：路由里不许再出现把异常原文塞进 detail 的写法。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "backend" / "api"
    bad = re.compile(r"detail=(?:str|repr)\((?:e|exc|err)\)|detail=f\"[^\"]*\{(?:e|exc|err)(?:!r)?\}")
    hits = [f"{p.name}:{i}" for p in root.glob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if bad.search(line)]
    assert not hits, hits
