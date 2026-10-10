"""数值/结构化答案确定性校验（408 非代码门的验证支柱）。

比对「结构化 token」而非散文：前端按题型给出干净的答案 token（数字框/ABCD/位串/子网），
canonical 答案以 JSON 存在 problems.answer（或 solve/practice 的 final_answer_canonical）。
只做确定性判定：能规范化就 True/False；无法解析或题目本身多解/需推导 → passed=None（安全边界）。

canonical kind：
  scalar   {"kind":"scalar","value":1.5,"tol":0.01,"unit":"","aliases":["3/2"]}   CPI/加速比/时延/吞吐
  percent  {"kind":"percent","value":0.375,"tol":0.001}                            命中率/利用率/缺页率
  int      {"kind":"int","value":12}                                               缺页数/道数/主机数
  choice   {"kind":"choice","value":"B"} | {"value":["A","C"]}                     选择题（统考主力）
  bits     {"kind":"bits","value":"11110110","base":2,"width":8}                   补码/原码/反码/CRC/海明码
  ipv4net  {"kind":"ipv4net","network":"192.168.1.0","prefix":24}                  子网号/掩码/网段
  set      {"kind":"set","values":[...],"order_matters":false}                     多值/安全序列
"""
from __future__ import annotations

import ipaddress
import logging
import re

from backend.verification.base import VerifyResult

_NONE = None  # 语义标注：无法判定
logger = logging.getLogger(__name__)

# details 会原样显示在练习页和模考复盘里，所以全部写成学生读得懂的话：
# 不出现 canonical / kind / base / 容差 这类内部字段名，也不回显 Python repr。
_UNCHECKABLE = "这道题暂时没法自动核对，以讲解为准"
_BASE_NAME = {2: "二进制", 8: "八进制", 10: "十进制", 16: "十六进制"}


def _ok(passed, details, **extra) -> VerifyResult:
    conf = 1.0 if passed is not None else 0.0
    return VerifyResult("numeric", passed, details, confidence=conf, extra=extra)


def _to_float(tok: str):
    """把 token 解析成 float：支持 '1.5'、'3/2' 分数、带单位后缀（剥非数字）。失败返回 None。"""
    if tok is None:
        return None
    s = str(tok).strip()
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*", s)
    if m:  # 分数
        a, b = float(m.group(1)), float(m.group(2))
        return a / b if b else None
    m = re.search(r"-?\d+(?:\.\d+)?", s)  # 取第一个数值（容忍单位/文字）
    return float(m.group()) if m else None


def _check_scalar(tok: str, c: dict) -> VerifyResult:
    aliases = [str(a).strip() for a in (c.get("aliases") or [])]
    if str(tok).strip() in aliases:
        return _ok(True, "答案正确（等价写法）")
    v = _to_float(tok)
    if v is None:
        return _ok(_NONE, "没有从作答里读出数值，请只填数字（可带单位）")
    target = float(c["value"])
    tol = float(c.get("tol", 0.0))
    passed = abs(v - target) <= tol + 1e-12
    unit = c.get("unit") or ""
    return _ok(passed, f"你的答案 {v:g}{unit}，标准答案 {target:g}{unit}" + (f"（允许误差 ±{tol:g}）" if tol else ""))


def _check_percent(tok: str, c: dict) -> VerifyResult:
    s = str(tok).strip()
    if "%" in s:
        v = _to_float(s.replace("%", ""))
        v = v / 100 if v is not None else None
    else:
        v = _to_float(s)  # 分数或小数
    if v is None:
        return _ok(_NONE, "没有从作答里读出百分比，请填如 37.5% 或 0.375")
    target = float(c["value"])
    tol = float(c.get("tol", 0.001))
    passed = abs(v - target) <= tol + 1e-12
    return _ok(passed, f"你的答案 {v * 100:g}%，标准答案 {target * 100:g}%")


def _check_int(tok: str, c: dict) -> VerifyResult:
    m = re.search(r"-?\d+", str(tok))
    if not m:
        return _ok(_NONE, "没有从作答里读出整数")
    v = int(m.group())
    passed = v == int(c["value"])
    return _ok(passed, f"你的答案 {v}，标准答案 {c['value']}")


def _letters(s: str) -> list[str]:
    return re.findall(r"[A-Za-z]", str(s))


def _check_choice(tok: str, c: dict) -> VerifyResult:
    gold = c["value"]
    gold_set = {x.strip().upper() for x in (gold if isinstance(gold, list) else [gold])}
    stu_set = {x.upper() for x in _letters(tok)}
    if not stu_set:
        return _ok(_NONE, "没有识别出选项字母，请填 A–D")
    passed = stu_set == gold_set
    return _ok(passed, f"你选了 {''.join(sorted(stu_set))}，正确答案是 {''.join(sorted(gold_set))}")


def _norm_bits(s: str, base: int) -> str | None:
    t = re.sub(r"\s+", "", str(s)).lower()
    t = re.sub(r"^0[bx]", "", t)
    try:
        val = int(t, base)
    except ValueError:
        return None
    return str(val)  # 用十进制值比较，规避前导零/宽度差异


def _check_bits(tok: str, c: dict) -> VerifyResult:
    base = int(c.get("base", 2))
    stu = _norm_bits(tok, base)
    gold = _norm_bits(str(c["value"]), base)
    if stu is None or gold is None:
        return _ok(_NONE, f"没有从作答里读出{_BASE_NAME.get(base, f'{base} 进制')}数")
    passed = stu == gold
    return _ok(passed, f"换算成十进制后，你的答案是 {stu}，标准答案是 {gold}")


def _parse_net(s: str):
    """从作答里解析出 ip 网络：支持 'a.b.c.d/n' 或 'a.b.c.d 掩码'。返回 ip_network 或 None。"""
    s = str(s).strip()
    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})\s*/\s*(\d{1,2})", s)
    if m:
        try:
            return ipaddress.ip_network(f"{m.group(1)}/{m.group(2)}", strict=False)
        except ValueError:
            return None
    ips = re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", s)
    if len(ips) >= 2:  # ip + 点分掩码
        try:
            return ipaddress.ip_network(f"{ips[0]}/{ips[1]}", strict=False)
        except ValueError:
            return None
    return None


def _check_ipv4net(tok: str, c: dict) -> VerifyResult:
    try:
        gold = ipaddress.ip_network(f"{c['network']}/{c['prefix']}", strict=False)
    except (ValueError, KeyError):
        return _ok(_NONE, "这道题暂时没法自动核对，以讲解为准")
    stu = _parse_net(tok)
    if stu is None:
        return _ok(_NONE, "没有从作答里读出网络地址，请填如 192.168.1.0/24")
    passed = (stu.network_address == gold.network_address and stu.prefixlen == gold.prefixlen)
    return _ok(passed, f"你的答案 {stu}，标准答案 {gold}")


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[,，、;；\s]+", str(s).strip()) if t]


def _check_set(tok: str, c: dict) -> VerifyResult:
    gold = [str(x).strip().upper() for x in c.get("values", [])]
    stu = [t.upper() for t in _tokens(tok)]
    if not stu:
        return _ok(_NONE, "没有从作答里读出任何元素，请用逗号分隔")
    if c.get("order_matters"):
        passed = stu == gold
    else:
        passed = set(stu) == set(gold)
    return _ok(passed, f"你的答案 {'、'.join(stu)}，标准答案 {'、'.join(gold)}" + ("（顺序也要一致）" if c.get("order_matters") else ""))


_DISPATCH = {
    "scalar": _check_scalar, "percent": _check_percent, "int": _check_int,
    "choice": _check_choice, "bits": _check_bits, "ipv4net": _check_ipv4net, "set": _check_set,
}


def check_numeric(student_token: str, canonical: dict | None) -> VerifyResult:
    """按 canonical.kind 分派确定性校验。canonical 为 None/未知 kind → 无法判定。"""
    if not isinstance(canonical, dict) or "kind" not in canonical:
        return _ok(_NONE, _UNCHECKABLE)
    fn = _DISPATCH.get(canonical["kind"])
    if fn is None:
        return _ok(_NONE, _UNCHECKABLE)
    try:
        return fn(student_token, canonical)
    except Exception:  # noqa: BLE001 —— 任意解析异常都退化为安全边界
        # 异常细节只进日志：details 会直接显示给学生
        logger.exception("numeric check failed: kind=%s", canonical.get("kind"))
        return _ok(_NONE, _UNCHECKABLE)


def canonical_text(canonical) -> str:
    """canonical 答案的可读写法，给"参考答案"这类要给人看的地方用。

    住在这里是因为本模块才是 canonical 各 kind 的定义处 —— 渲染规则跟着定义走，
    以后加了新的 kind，加校验和加写法在同一个文件里，不会只加一半。
    直接把 JSON 串印给学生的话，看到的是 {"kind":"choice","value":"B"}。
    """
    if not isinstance(canonical, dict):
        return str(canonical or "")
    kind = canonical.get("kind")
    if kind == "percent":
        try:
            return f"{float(canonical.get('value', 0)) * 100:g}%"
        except (TypeError, ValueError):
            return str(canonical.get("value", ""))
    if kind == "ipv4net":
        return f"{canonical.get('network', '')}/{canonical.get('prefix', '')}"
    if kind == "set":
        return "、".join(str(v) for v in (canonical.get("values") or []))
    value = canonical.get("value")
    if isinstance(value, list):
        return "".join(str(v) for v in value)
    unit = canonical.get("unit") or ""
    return f"{value}{unit}" if value is not None else ""
