"""沙箱代码运行器 + 测试样例验证。

安全边界（比赛/教学环境）：子进程执行，施加 CPU 时间与内存上限（resource），
超时强杀，禁止交互输入。⚠️ 未做网络隔离与文件系统沙箱——仅用于运行课程内可信/半可信的
DS&A 代码片段；生产环境应换 nsjail/容器/seccomp。

用法：
    r = run_python_tests(code, func="two_sum",
                         cases=[{"args": [[2,7,11], 9], "expected": [0,1]}])
    r.passed  # True / False / None
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from backend.verification import code_errors as ce
from backend.verification.base import VerifyResult

logger = logging.getLogger(__name__)

DEFAULT_TIME_LIMIT = 5      # 秒（CPU）
DEFAULT_MEM_MB = 256        # 内存上限

# 子进程内的测试驱动：读入 code + cases，逐条调用 func(*args) 与 expected 比对，输出 JSON
_HARNESS = r'''
import json, sys, resource, io, contextlib

def _limit(cpu, mem_mb):
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        b = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (b, b))
    except Exception:
        pass

payload = json.loads(sys.stdin.read())
_limit(payload["cpu"], payload["mem_mb"])
code, func, cases = payload["code"], payload["func"], payload["cases"]

ns = {}
results = []
try:
    with contextlib.redirect_stdout(io.StringIO()):
        exec(code, ns)
except BaseException as e:
    # 只回报结构化信息（类名 / 原文 / 行号），给人看的中文由父进程按错误码翻译
    print(json.dumps({"ok": False, "code": "syntax_error" if isinstance(e, SyntaxError) else "load_error",
                      "exc": type(e).__name__, "msg": str(getattr(e, "msg", "") or e),
                      "line": getattr(e, "lineno", None), "results": []}))
    sys.exit(0)

fn = ns.get(func)
if not callable(fn):
    print(json.dumps({"ok": False, "code": "func_missing", "results": []}))
    sys.exit(0)

for i, c in enumerate(cases):
    args = c.get("args", [])
    exp = c.get("expected")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            got = fn(*args)
        ok = (got == exp)
        results.append({"i": i, "passed": bool(ok), "got": repr(got), "expected": repr(exp),
                        "hidden": bool(c.get("hidden"))})
    except Exception as e:
        results.append({"i": i, "passed": False, "exc": type(e).__name__, "msg": str(e),
                        "hidden": bool(c.get("hidden"))})

print(json.dumps({"ok": True, "results": results}))
'''


def run_python_tests(code: str, func: str, cases: list[dict],
                     time_limit: int = DEFAULT_TIME_LIMIT,
                     mem_mb: int = DEFAULT_MEM_MB) -> VerifyResult:
    """在子进程沙箱里运行 code，用 cases 测试 func。返回 VerifyResult。"""
    if not code or not func or not cases:
        return VerifyResult("unit_test", None, "缺少代码/函数名/测试样例，无法验证", confidence=0.0)

    with tempfile.TemporaryDirectory() as d:
        harness = Path(d) / "h.py"
        harness.write_text(_HARNESS, encoding="utf-8")
        payload = json.dumps({"code": code, "func": func, "cases": cases,
                              "cpu": time_limit, "mem_mb": mem_mb})
        try:
            proc = subprocess.run(
                [sys.executable, str(harness)],
                input=payload, capture_output=True, text=True,
                timeout=time_limit + 2,
            )
        except subprocess.TimeoutExpired:
            return _fail(False, ce.TIMEOUT, f"运行超时（超过 {time_limit} 秒），可能有死循环或复杂度过高",
                         f"TimeoutExpired: >{time_limit}s")

    # details 会原样显示给学生和助教（练习页、模考复盘、错题的"AI 判定"），所以只放中文；
    # 异常类名、原文、沙箱 stderr 放进 extra["tech"] 并写日志（测试报告 I-05）。
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        # 无输出：多为被 RLIMIT_CPU/RLIMIT_AS 强杀（负 returncode = 信号）
        if proc.returncode and proc.returncode < 0:
            return _fail(False, ce.KILLED, ce.KILLED_TEXT, f"killed by signal {-proc.returncode}")
        return _fail(None, ce.SANDBOX, ce.SANDBOX_TEXT, f"no output; stderr={(proc.stderr or '')[:500]}")
    try:
        data = json.loads(out[-1])
    except Exception:
        return _fail(None, ce.SANDBOX, ce.SANDBOX_TEXT,
                     f"unparseable output; {(proc.stderr or proc.stdout or '')[:500]}")

    if not data.get("ok"):
        code = data.get("code") or ce.LOAD
        if code == ce.FUNC_MISSING:
            return _fail(False, code, ce.func_missing_text(func), f"function {func!r} not found")
        exc, msg, line = data.get("exc") or "", data.get("msg") or "", data.get("line")
        tech = f"{exc}: {msg}" + (f" (line {line})" if line else "")
        return _fail(False, code, ce.load_failure_text(exc, msg, line), tech)

    results = data["results"]
    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    all_ok = passed == total
    # 首个失败样例（非 hidden 优先给细节）
    fail = next((r for r in results if not r.get("passed")), None)
    if all_ok:
        details = f"全部 {total} 个样例通过"
    elif fail is not None:
        if fail.get("hidden"):
            details = f"{passed}/{total} 通过；隐藏样例未通过"
        elif fail.get("exc"):
            details = (f"{passed}/{total} 通过；"
                       + ce.case_failure_text(fail["i"] + 1, fail["exc"], fail.get("msg") or ""))
            return VerifyResult("unit_test", False, details, confidence=1.0,
                                extra={"passed": passed, "total": total, "code": ce.RUNTIME,
                                       "tech": f"case {fail['i'] + 1}: {fail['exc']}: {fail.get('msg') or ''}"})
        else:
            details = (f"{passed}/{total} 通过；样例 {fail['i']+1} 未通过："
                       f"得到 {fail.get('got')}，期望 {fail.get('expected')}")
    else:
        details = f"{passed}/{total} 通过"
    return VerifyResult("unit_test", all_ok, details, confidence=1.0,
                        extra={"passed": passed, "total": total})


def _fail(passed, code: str, text: str, tech: str) -> VerifyResult:
    logger.info("unit test run failed: code=%s tech=%s", code, tech)
    return VerifyResult("unit_test", passed, text, confidence=1.0 if passed is False else 0.0,
                        extra={"code": code, "tech": tech})
