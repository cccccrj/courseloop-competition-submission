"""CourseLoop 500 条合成数据小试：任务路由、双模型核对、确定性过滤。

输出与旧 SFT 完全隔离在 ``data/synthetic_pilot_500``；候选、接受样本和
调用记录都按条追加，可断点续跑。脚本只读环境中的智增增密钥，不落盘密钥。

规模：150 practice(Qwen) + 150 diagnose(Kimi) + 100 grounded QA(Kimi)
      + 100 solve(Kimi/Qwen 同题同意) = 500 个候选、600 次 API 请求。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import random
import re
import statistics
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from backend.agents.prompts import (
    GROUNDED_QA_SYSTEM,
    build_diagnose_system,
    build_practice_system,
    build_solve_system,
)
from backend.core.config import settings
from backend.core.courses import get_course
from backend.learning.error_taxonomy import ERROR_CODES, normalize
from backend.verification.numeric_checker import check_numeric
from backend.verification.unit_test_runner import run_python_tests
from scripts.teacher_bakeoff import _answer_contains_canonical, _canonical_token, _client
from scripts.test_lora import _extract_json


HERE = Path(__file__).resolve().parents[1]
OUT_DIR = HERE / "data" / "synthetic_pilot_500"
RESULTS_DIR = OUT_DIR / "candidates"
ACCEPTED_FILE = OUT_DIR / "accepted.jsonl"
REPORT_JSON = OUT_DIR / "report.json"
REPORT_MD = OUT_DIR / "report.md"
REVIEW_FILE = OUT_DIR / "review_50.jsonl"
MANIFEST_FILE = OUT_DIR / "manifest.json"

COURSES = ("co", "ds-algo", "net", "os")
KIMI = "kimi-k2.5"
QWEN = "qwen3-max-2025-09-23"
TARGETS = {"practice": 150, "diagnose": 150, "grounded_qa": 100, "solve": 100}
PRICES = {KIMI: (4.0, 21.0), QWEN: (6.0, 24.0)}  # 元 / 1M input, output tokens
ERROR_TYPES = ("boundary", "reasoning", "strategy", "concept_confuse", "complexity", "calculation")

GATE = {
    "api_success_rate_min": 99.0,
    "schema_rate_min": 98.0,
    "retention_rate_min": 80.0,
    "task_accept_rate_min": {
        "practice": 80.0, "diagnose": 80.0, "grounded_qa": 90.0, "solve": 85.0,
    },
    "duplicate_rate_max": 2.0,
    "manual_review_n": 50,
    "manual_accept_rate_min": 90.0,
    "manual_severe_errors_max": 0,
    "bulk_first_accepted": 3000,
    "bulk_max_accepted": 5000,
}

QA_SYSTEM = GROUNDED_QA_SYSTEM

_LOCK = threading.Lock()
_TLS = threading.local()


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _norm(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text).lower())


def _choice_matches(answer: str, stem: str, canonical: dict) -> bool:
    """选择题既允许返回字母，也允许返回正确选项内容。

    不能用“提取散文所有英文字母”判定，否则 CPU/BGP 等缩写会误杀。
    """
    gold = canonical.get("value")
    if isinstance(gold, list):
        return check_numeric(answer, canonical).passed is True
    gold = str(gold).strip().upper()
    lead = re.search(r"^\s*(?:答案|故选|选择)?\s*[:：]?\s*([A-D])(?:\s|[.、:：，,])", answer, re.I)
    if lead:
        return lead.group(1).upper() == gold
    options = {}
    matches = list(re.finditer(r"(?m)(?:^|\s)([A-D])[.、:：]\s*", stem))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(stem)
        options[match.group(1).upper()] = stem[match.end():end].strip()
    option = options.get(gold, "")
    return bool(option and (_norm(option) in _norm(answer) or _norm(answer) in _norm(option)))


def _verify_noncode_solution(parsed: Any, problem: dict) -> tuple[bool, str]:
    if not isinstance(parsed, dict):
        return False, "invalid_json"
    canonical = problem.get("final_answer_canonical")
    if not isinstance(canonical, dict):
        return False, "missing_canonical"
    answer = str(parsed.get("final_answer", ""))
    if canonical.get("kind") == "choice" and _choice_matches(answer, str(problem.get("stem", "")), canonical):
        return True, "choice letter/content matched"
    predicted = parsed.get("final_answer_canonical")
    token = (_canonical_token(predicted) if isinstance(predicted, dict) and
             predicted.get("kind") == canonical.get("kind") else answer)
    vr = check_numeric(token, canonical)
    return vr.passed is True, vr.details


def _evidence_overlap(answer: str, evidence: str) -> float:
    """对 PDF 公式/中英混排友好的轻量字面重叠，不替代严格原文引用检查。"""
    def tokens(text: str) -> set[str]:
        text = str(text).lower()
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        grams = {chinese[i:i + 2] for i in range(max(0, len(chinese) - 1))}
        return grams | set(re.findall(r"[a-z]+\d*|\d+(?:\.\d+)?", text))
    left, right = tokens(answer), tokens(evidence)
    return len(left & right) / max(1, len(left))


_SELF_CORRECTION_MARKERS = (
    "重新检查", "重新计算", "修正题干", "修改题干",
    "发现错误", "发现矛盾", "选项中没有", "选项不匹配", "与选项不符",
    "题目设计有误", "题目有误", "为了使答案", "为使答案",
)


def _practice_content_reasons(parsed: Any, code_enabled: bool) -> list[str]:
    """Catch unmistakable draft leakage before an expensive semantic audit.

    This deliberately targets only strong signals.  It does not pretend to prove the
    mathematical answer; the independent judge remains the semantic authority.
    """
    if not isinstance(parsed, dict):
        return []
    public_text = "\n".join(str(parsed.get(key) or "") for key in ("stem", "answer", "hint"))
    reasons = []
    if any(marker in public_text for marker in _SELF_CORRECTION_MARKERS):
        reasons.append("draft_or_self_correction_leakage")
    if code_enabled:
        topic_text = " ".join(map(str, parsed.get("knowledge_points") or [])) + " " + public_text
        code = str(parsed.get("code") or "")
        structural_topic = re.search(r"红黑树|B\+?树|平衡树|AVL|图遍历|最短路|拓扑排序", topic_text, re.I)
        bypass = re.search(r"不需要实现|无需实现|只需(?:使用|调用|返回)|等价.*排序", public_text)
        if structural_topic and bypass and re.search(r"\bsorted\s*\(", code):
            reasons.append("topic_bypassed_by_trivial_shortcut")
    return reasons


def _qa_valid(parsed: Any, source_id: str, context: str, expected_abstain: bool) -> tuple[bool, str]:
    if not isinstance(parsed, dict):
        return False, "invalid_json"
    if expected_abstain:
        ok = parsed.get("abstain") is True and not parsed.get("citations")
        return ok, "clean_abstention" if ok else "bad_abstention"
    citations = parsed.get("citations") or []
    normalized_context = re.sub(r"\s+", "", context)
    exact_evidence = []
    for citation in citations:
        if not isinstance(citation, dict) or citation.get("source_id") != source_id:
            continue
        evidence = str(citation.get("evidence") or "")
        normalized = re.sub(r"\s+", "", evidence)
        if normalized and normalized in normalized_context:
            exact_evidence.append(evidence)
    answer = str(parsed.get("answer") or "")
    evidence_text = "\n".join(exact_evidence)
    lexical = bool(answer) and (len(answer) <= 20 or _evidence_overlap(answer, evidence_text) >= 0.08)
    ok = parsed.get("abstain") is False and bool(exact_evidence) and lexical
    return ok, f"exact={bool(exact_evidence)}, overlap={round(_evidence_overlap(answer, evidence_text), 3)}"


def _pct(n: float, d: int) -> float:
    return round(100 * n / d, 2) if d else 0.0


def _get_client():
    if not hasattr(_TLS, "client"):
        _TLS.client = _client()
    return _TLS.client


def _call(model: str, messages: list[dict], temperature: float, max_tokens: int) -> dict:
    started = time.perf_counter()
    raw = ""
    error = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    try:
        response = _get_client().chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
        )
        raw = response.choices[0].message.content or ""
        obj = getattr(response, "usage", None)
        if obj:
            usage = {k: int(getattr(obj, k, 0) or 0) for k in usage}
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    return {
        "model": model, "raw_output": raw, "error": error, "usage": usage,
        "latency_seconds": round(time.perf_counter() - started, 3),
    }


def _slots(n: int, prefix: str) -> list[dict]:
    counts = [n // 4 + (1 if i < n % 4 else 0) for i in range(4)]
    out = []
    for course, count in zip(COURSES, counts):
        topics = list(get_course(course).topics)
        for i in range(count):
            out.append({
                "candidate_id": f"{prefix}-{course}-{i+1:03d}", "course": course,
                "topic": topics[(i * 7 + 3) % len(topics)], "difficulty": 1 + (i % 4),
            })
    return out


def _practice(slot: dict) -> dict:
    course = slot["course"]
    cobj = get_course(course)
    constraint = (
        "必须是可运行 Python 代码题；给出 code、func_name 和至少 3 条 JSON 测试，"
        "test_cases 中只能使用合法 JSON（null/true/false，不能写 None/True/False）。"
        if cobj.code_enabled else
        "必须有唯一答案和 final_answer_canonical。若 kind=choice，题干必须列出 A-D 选项，answer 必须明说选项字母。"
    )
    course_guard = {
        "os": (
            "操作系统题额外约束：页表必须覆盖题干出现的全部页号并明确初始驻留页/物理块数；"
            "缓冲题必须明确设备能否并行、缓冲何时释放；死锁题必须保证只有一个选项成立。"
        ),
        "co": (
            "计算机组成题额外约束：用题干原始数值独立复算，正确结果必须原样出现在选项中；"
            "不得为匹配选项而在解析中替换地址、位宽、CPI或其他条件。"
        ),
        "ds-algo": (
            "数据结构代码题额外约束：测试必须覆盖指定结构/算法的关键行为和边界，"
            "参考实现不得把红黑树、B/B+树、图算法等退化成直接排序或硬编码。"
        ),
        "net": (
            "网络题额外约束：掩码、地址范围、序号窗口和协议状态必须按题干数值复核，"
            "正确结果必须与选项及final_answer_canonical一致。"
        ),
    }[course]
    messages = [
        {"role": "system", "content": build_practice_system(cobj) +
         "\n只输出合法 JSON，不要 Markdown 代码块。题目必须自包含且可独立验证。"
         "请先在内部完整复算并校对题干、选项、答案、rubric和测试，但最终JSON中不得出现自我修正、"
         "改题过程或选项不匹配等草稿痕迹。代码题必须实质考查指定知识点，不得用sorted等捷径绕过核心算法。"},
        {"role": "user", "content":
         f"课程：{course}\n知识点：{slot['topic']}\n难度：{slot['difficulty']}/5\n{constraint}\n"
         f"{course_guard}\n"
         "请生成一道不依赖未提供图表、具有新数值或新场景的变式题。"},
    ]
    call = _call(QWEN, messages, 0.7, 3000 if cobj.code_enabled else 1800)
    parsed = _extract_json(call["raw_output"])
    reasons = []
    required = {"stem", "type", "difficulty", "knowledge_points", "answer", "hint"}
    schema_ok = isinstance(parsed, dict) and required.issubset(parsed)
    if not schema_ok:
        reasons.append("schema_or_json")
    if schema_ok:
        reasons.extend(_practice_content_reasons(parsed, cobj.code_enabled))
        if cobj.code_enabled:
            code, func, tests = parsed.get("code"), parsed.get("func_name"), parsed.get("test_cases")
            if not (code and func and isinstance(tests, list) and len(tests) >= 3):
                reasons.append("missing_code_or_tests")
            elif run_python_tests(str(code), str(func), tests).passed is not True:
                reasons.append("code_tests_failed")
        else:
            canonical = parsed.get("final_answer_canonical")
            if not isinstance(canonical, dict):
                reasons.append("missing_canonical")
            elif not _answer_contains_canonical(str(parsed.get("answer", "")), canonical):
                reasons.append("answer_canonical_mismatch")
            if isinstance(canonical, dict) and canonical.get("kind") == "choice":
                stem = str(parsed.get("stem", ""))
                if not all(re.search(rf"(?m)(^|\s){x}[.、:]", stem) for x in "ABCD"):
                    reasons.append("choice_options_missing")
    accepted = not call["error"] and not reasons
    sample = None
    if accepted:
        sample = {"messages": messages + [{"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)}],
                  "course": course, "intent": "practice", "synthetic_pilot": True}
    return _record(slot, "practice", [call], parsed, schema_ok, accepted, reasons, sample)


def _solve(slot: dict, seed: dict) -> dict:
    course = seed["course"]
    problem = seed["parsed"]
    cobj = get_course(course)
    messages = [
        {"role": "system", "content": build_solve_system(cobj) +
         "\n只输出合法 JSON，不要 Markdown 代码块。"},
        {"role": "user", "content": f"题目：{problem['stem']}\n\n请按要求解题。"},
    ]
    calls = [_call(KIMI, messages, 0.2, 3000), _call(QWEN, messages, 0.2, 3000)]
    parsed = [_extract_json(c["raw_output"]) for c in calls]
    required = {"problem_type", "knowledge_points", "givens", "target", "strategy",
                "steps", "final_answer", "common_errors"}
    schema_flags = [isinstance(p, dict) and required.issubset(p) for p in parsed]
    passes = []
    details = []
    if cobj.code_enabled:
        tests = problem.get("test_cases") or []
        for p in parsed:
            if not isinstance(p, dict) or not p.get("code") or not p.get("func_name"):
                passes.append(False); details.append("missing_code")
            else:
                vr = run_python_tests(str(p["code"]), str(p["func_name"]), tests)
                passes.append(vr.passed is True); details.append(vr.details)
    else:
        for p in parsed:
            passed, detail = _verify_noncode_solution(p, problem)
            passes.append(passed); details.append(detail)
    reasons = []
    if not all(schema_flags): reasons.append("schema_or_json")
    if not all(passes): reasons.append("teacher_disagreement_or_wrong")
    if any(c["error"] for c in calls): reasons.append("api_error")
    accepted = not reasons
    sample = None
    if accepted:
        sample = {"messages": messages + [{"role": "assistant", "content": json.dumps(parsed[0], ensure_ascii=False)}],
                  "course": course, "intent": "solve", "synthetic_pilot": True,
                  "verified_by": [KIMI, QWEN], "source_candidate_id": seed["candidate_id"]}
    meta = dict(slot, course=course, topic=seed.get("topic"), source_candidate_id=seed["candidate_id"])
    row = _record(meta, "solve", calls, parsed[0], all(schema_flags), accepted, reasons, sample)
    row["teacher_pass"] = dict(zip((KIMI, QWEN), passes))
    row["teacher_details"] = dict(zip((KIMI, QWEN), details))
    return row


def _diagnose(slot: dict, seed: dict) -> dict:
    course = seed["course"]
    problem = seed["parsed"]
    cobj = get_course(course)
    error_type = slot["error_type"]
    code_note = (
        "student_code 必须保持原 func_name，且至少失败一条给定测试。"
        if cobj.code_enabled else
        "student_final_answer 必须是可机器判定且与规范答案不同的错误最终答案。"
    )
    system = (
        f"你是{cobj.display}教学数据构造与错因诊断助手。针对给定题目与参考答案，"
        f"构造一份第1步就犯 {error_type} 错误的学生作答，然后给出诊断。{code_note}"
        "只输出合法 JSON，字段：student_steps(字符串数组)、student_answer、student_final_answer、"
        "student_code、correct(false)、score、first_wrong_step、error_tags、diagnosis、remediation、reexplain。"
        f"error_tags 必须严格等于 [\"{error_type}\"]，不得翻译、改写或增加其他标签。"
    )
    reference = problem.get("answer") or problem.get("code") or ""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content":
         f"题目：{problem['stem']}\n\n参考答案：{reference}\n"
         f"规范答案：{json.dumps(problem.get('final_answer_canonical'), ensure_ascii=False)}\n"
         f"参考函数名：{problem.get('func_name', '')}\n"
         f"测试：{json.dumps(problem.get('test_cases') or [], ensure_ascii=False)}"},
    ]
    call = _call(KIMI, messages, 0.6, 2200)
    parsed = _extract_json(call["raw_output"])
    required = {"student_steps", "student_answer", "correct", "score", "first_wrong_step",
                "error_tags", "diagnosis", "remediation", "reexplain"}
    schema_ok = isinstance(parsed, dict) and required.issubset(parsed)
    reasons = []
    if not schema_ok:
        reasons.append("schema_or_json")
    if schema_ok:
        tags = {normalize(str(x)) for x in (parsed.get("error_tags") or [])}
        if error_type not in tags or any(x not in ERROR_CODES for x in tags):
            reasons.append("wrong_error_tag")
        if parsed.get("correct") is not False or not isinstance(parsed.get("first_wrong_step"), int):
            reasons.append("invalid_diagnosis_label")
        if cobj.code_enabled:
            scode = str(parsed.get("student_code") or "")
            if not scode:
                reasons.append("missing_wrong_code")
            elif run_python_tests(scode, str(problem.get("func_name") or ""), problem.get("test_cases") or []).passed is not False:
                reasons.append("student_code_not_provably_wrong")
        else:
            canonical = problem.get("final_answer_canonical")
            wrong = str(parsed.get("student_final_answer") or "")
            if not wrong or check_numeric(wrong, canonical).passed is not False:
                reasons.append("student_answer_not_provably_wrong")
    accepted = not call["error"] and not reasons
    sample = None
    if accepted:
        steps = "\n".join(f"第{i+1}步：{x}" for i, x in enumerate(parsed["student_steps"]))
        student = steps + "\n最终答案：" + str(parsed.get("student_final_answer") or parsed["student_answer"])
        if cobj.code_enabled:
            student += "\n学生代码：\n" + str(parsed.get("student_code") or "")
        train_user = f"题目：{problem['stem']}\n\n参考答案：{reference}\n\n学生作答：{student}\n\n请定位第一个错误步骤并诊断错因："
        assistant = {k: parsed[k] for k in ("correct", "score", "first_wrong_step", "error_tags",
                                              "diagnosis", "remediation", "reexplain")}
        sample = {"messages": [{"role": "system", "content": build_diagnose_system(cobj)},
                                {"role": "user", "content": train_user},
                                {"role": "assistant", "content": json.dumps(assistant, ensure_ascii=False)}],
                  "course": course, "intent": "diagnose", "synthetic_pilot": True,
                  "source_candidate_id": seed["candidate_id"]}
    meta = dict(slot, course=course, topic=seed.get("topic"), source_candidate_id=seed["candidate_id"])
    return _record(meta, "diagnose", [call], parsed, schema_ok, accepted, reasons, sample)


def _qa(slot: dict) -> dict:
    source_id, context, course = slot["source_id"], slot["context"], slot["course"]
    if slot["expected_abstain"]:
        task = (f"固定问题：{slot['question']}\n对该问题作答。材料不支持时 abstain=true 且 citations=[]。")
    else:
        task = ("先根据材料生成一个具体、可由材料充分回答的问题，再回答。"
                "不要问‘归纳两个要点’这类泛题。")
    messages = [
        {"role": "system", "content": QA_SYSTEM +
         '\n只输出合法JSON：{"question":"","answer":"","abstain":false,'
         '"citations":[{"source_id":"","evidence":""}]}。'},
        {"role": "user", "content": f"source_id: {source_id}\n课程: {course}\n材料：\n{context}\n\n{task}"},
    ]
    call = _call(KIMI, messages, 0.4, 1800)
    parsed = _extract_json(call["raw_output"])
    required = {"question", "answer", "abstain", "citations"}
    schema_ok = isinstance(parsed, dict) and required.issubset(parsed)
    reasons = []
    if not schema_ok:
        reasons.append("schema_or_json")
    if schema_ok:
        valid, detail = _qa_valid(parsed, source_id, context, slot["expected_abstain"])
        if not valid:
            reasons.append(detail)
    accepted = not call["error"] and not reasons
    sample = None
    if accepted:
        question = slot["question"] if slot["expected_abstain"] else parsed["question"]
        train_user = f"source_id: {source_id}\n课程: {course}\n材料：\n{context}\n\n问题：{question}"
        assistant = {k: parsed[k] for k in ("answer", "abstain", "citations")}
        sample = {"messages": [{"role": "system", "content": QA_SYSTEM},
                                {"role": "user", "content": train_user},
                                {"role": "assistant", "content": json.dumps(assistant, ensure_ascii=False)}],
                  "course": course, "intent": "grounded_qa", "synthetic_pilot": True,
                  "source_id": source_id}
    return _record(slot, "grounded_qa", [call], parsed, schema_ok, accepted, reasons, sample,
                   drop=("context",))


def _record(slot: dict, task: str, calls: list[dict], parsed: Any, schema_ok: bool,
            accepted: bool, reasons: list[str], sample: dict | None,
            drop: tuple[str, ...] = ()) -> dict:
    meta = {k: v for k, v in slot.items() if k not in drop}
    return {
        **meta, "task": task, "calls": calls, "parsed": parsed, "schema_ok": bool(schema_ok),
        "accepted": bool(accepted), "reject_reasons": sorted(set(reasons)),
        "training_sample": sample, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _qa_slots() -> list[dict]:
    import chromadb

    collection = chromadb.PersistentClient(path=str(HERE / "data" / "processed" / "chroma")).get_collection(
        settings.chroma_collection
    )
    foreign = {
        "co": "材料是否说明 TCP 三次握手的报文标志位？",
        "ds-algo": "材料是否说明 RAID 5 的容错原理？",
        "net": "材料是否说明虚拟内存的 Clock 置换算法？",
        "os": "材料是否说明红黑树插入修复的旋转规则？",
    }
    out = []
    for course in COURSES:
        got = collection.get(where={"course": course}, limit=500, include=["documents", "metadatas"])
        rows = []
        for cid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"]):
            text = (doc or "").strip()
            title = str((meta or {}).get("section") or (meta or {}).get("title_path") or
                        (meta or {}).get("chapter") or (meta or {}).get("source") or "")
            if not (600 <= len(text) <= 2200 and title):
                continue
            if re.search(r"目录|致谢|如何使用|作者|Release", title + text[:100]):
                continue
            if text.count(". . .") >= 3:
                continue
            rows.append((cid, text[:1800], title))
        rows.sort(key=lambda x: _sha([course, x[0]]))
        if len(rows) < 25:
            raise RuntimeError(f"{course} 可用 RAG chunk 不足 25：{len(rows)}")
        for i, (cid, context, title) in enumerate(rows[:25], 1):
            abstain = i in (12, 24)
            out.append({"candidate_id": f"qa-{course}-{i:03d}", "course": course,
                        "source_id": cid, "context": context, "title": title,
                        "expected_abstain": abstain, "question": foreign[course] if abstain else ""})
    return out


def _run_stage(name: str, slots: list[dict], fn, workers: int) -> list[dict]:
    path = RESULTS_DIR / f"{name}.jsonl"
    existing = _jsonl(path)
    done = {r["candidate_id"] for r in existing}
    pending = [s for s in slots if s["candidate_id"] not in done]
    print(f"[{name}] 已有 {len(existing)}，待跑 {len(pending)} / {len(slots)}", flush=True)
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fn, slot): slot for slot in pending}
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                slot = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001
                    row = _record(slot, name, [], None, False, False,
                                  [f"worker:{type(exc).__name__}:{str(exc)[:300]}"], None)
                _append(path, row)
                completed += 1
                print(f"\r[{name}] {completed}/{len(pending)} | accepted={row['accepted']}   ", end="", flush=True)
        print()
    return _jsonl(path)


def _dependent_slots(practice_rows: list[dict]) -> tuple[list[dict], list[dict], dict[str, dict]]:
    accepted = sorted((r for r in practice_rows if r["accepted"]), key=lambda r: r["candidate_id"])
    if len(accepted) < 100:
        raise RuntimeError(f"practice 通过仅 {len(accepted)} < 100，不启动依赖阶段")
    by_id = {r["candidate_id"]: r for r in accepted}
    solve_slots = []
    diagnose_slots = []
    solve_counts = {course: 25 for course in COURSES}
    diagnose_counts = {course: 150 // 4 + (1 if i < 150 % 4 else 0)
                       for i, course in enumerate(COURSES)}
    for course in COURSES:
        course_rows = [r for r in accepted if r["course"] == course]
        if len(course_rows) < solve_counts[course]:
            raise RuntimeError(f"{course} practice 通过 {len(course_rows)} < 25")
        for i, seed in enumerate(course_rows[:solve_counts[course]], 1):
            solve_slots.append({"candidate_id": f"solve-{course}-{i:03d}", "seed_id": seed["candidate_id"]})
        for i in range(diagnose_counts[course]):
            seed = course_rows[i % len(course_rows)]
            diagnose_slots.append({"candidate_id": f"diagnose-{course}-{i+1:03d}",
                                   "seed_id": seed["candidate_id"],
                                   "error_type": ERROR_TYPES[i % len(ERROR_TYPES)]})
    return solve_slots, diagnose_slots, by_id


def _balance_legacy_solve(solve_rows: list[dict], practice_rows: list[dict], seeds: dict[str, dict],
                          workers: int) -> list[dict]:
    """早期小试已生成了不均衡的 100 条；只补各课程不足 25 的部分。"""
    slots = []
    for course in COURSES:
        have_rows = [r for r in solve_rows if r.get("course") == course]
        need = max(0, 25 - len(have_rows))
        used = {r.get("source_candidate_id") for r in have_rows}
        available = [r for r in practice_rows if r.get("accepted") and r["course"] == course and
                     r["candidate_id"] not in used]
        if len(available) < need:
            raise RuntimeError(f"{course} 无法补齐 solve：需 {need}，可用 {len(available)}")
        for i, seed in enumerate(available[:need], 1):
            slots.append({"candidate_id": f"solve-balance-{course}-{i:03d}",
                          "seed_id": seed["candidate_id"]})
    if slots:
        solve_rows = _run_stage("solve", slots, lambda s: _solve(s, seeds[s["seed_id"]]), workers)
    return solve_rows


def _selected_pilot_rows(rows: list[dict]) -> list[dict]:
    """从中断前的过量/不均衡候选中固定选出按计划分布的 500 条。"""
    selected = [r for r in rows if r.get("task") in {"practice", "grounded_qa"}]
    diagnoses = [r for r in rows if r.get("task") == "diagnose" and
                 re.match(r"^diagnose-(?:co|ds-algo|net|os)-\d+$", r.get("candidate_id", ""))]
    selected.extend(sorted(diagnoses, key=lambda r: r["candidate_id"])[:150])
    solve = [r for r in rows if r.get("task") == "solve"]
    for course in COURSES:
        course_rows = sorted((r for r in solve if r.get("course") == course),
                             key=lambda r: (r.get("created_at", ""), r["candidate_id"]))
        selected.extend(course_rows[:25])
    return selected


def _revalidate_solve(rows: list[dict], practice_rows: list[dict]) -> None:
    """用当前验证器重放已保存的双模型原始输出，不发起 API 请求。"""
    seeds = {r["candidate_id"]: r for r in practice_rows if r.get("accepted")}
    for row in rows:
        if row.get("task") != "solve" or row.get("source_candidate_id") not in seeds:
            continue
        seed = seeds[row["source_candidate_id"]]
        problem, course = seed["parsed"], seed["course"]
        parsed = [_extract_json(c.get("raw_output", "")) for c in row.get("calls", [])]
        if len(parsed) != 2:
            continue
        required = {"problem_type", "knowledge_points", "givens", "target", "strategy",
                    "steps", "final_answer", "common_errors"}
        schema_flags = [isinstance(p, dict) and required.issubset(p) for p in parsed]
        if get_course(course).code_enabled:
            passes, details = [], []
            for p in parsed:
                if not isinstance(p, dict) or not p.get("code") or not p.get("func_name"):
                    passes.append(False); details.append("missing_code")
                else:
                    vr = run_python_tests(str(p["code"]), str(p["func_name"]), problem.get("test_cases") or [])
                    passes.append(vr.passed is True); details.append(vr.details)
        else:
            checked = [_verify_noncode_solution(p, problem) for p in parsed]
            passes = [x[0] for x in checked]; details = [x[1] for x in checked]
        reasons = []
        if not all(schema_flags): reasons.append("schema_or_json")
        if not all(passes): reasons.append("teacher_disagreement_or_wrong")
        if any(c.get("error") for c in row.get("calls", [])): reasons.append("api_error")
        row["schema_ok"] = all(schema_flags)
        row["accepted"] = not reasons
        row["reject_reasons"] = reasons
        row["teacher_pass"] = dict(zip((KIMI, QWEN), passes))
        row["teacher_details"] = dict(zip((KIMI, QWEN), details))
        if row["accepted"]:
            cobj = get_course(course)
            messages = [{"role": "system", "content": build_solve_system(cobj) +
                         "\n只输出合法 JSON，不要 Markdown 代码块。"},
                        {"role": "user", "content": f"题目：{problem['stem']}\n\n请按要求解题。"}]
            row["training_sample"] = {
                "messages": messages + [{"role": "assistant", "content": json.dumps(parsed[0], ensure_ascii=False)}],
                "course": course, "intent": "solve", "synthetic_pilot": True,
                "verified_by": [KIMI, QWEN], "source_candidate_id": seed["candidate_id"],
            }
        else:
            row["training_sample"] = None


def _revalidate_qa(rows: list[dict]) -> None:
    """使用保存的原始输出和固定 RAG slot 离线重放 QA 门禁。"""
    slots = {slot["candidate_id"]: slot for slot in _qa_slots()}
    required = {"question", "answer", "abstain", "citations"}
    for row in rows:
        if row.get("task") != "grounded_qa" or row.get("candidate_id") not in slots:
            continue
        slot = slots[row["candidate_id"]]
        call = (row.get("calls") or [{}])[0]
        parsed = _extract_json(call.get("raw_output", ""))
        schema_ok = isinstance(parsed, dict) and required.issubset(parsed)
        reasons = []
        if not schema_ok:
            reasons.append("schema_or_json")
        else:
            valid, detail = _qa_valid(parsed, slot["source_id"], slot["context"], slot["expected_abstain"])
            if not valid:
                reasons.append(detail)
        if call.get("error"):
            reasons.append("api_error")
        row["parsed"] = parsed
        row["schema_ok"] = schema_ok
        row["accepted"] = not reasons
        row["reject_reasons"] = reasons
        if row["accepted"]:
            question = slot["question"] if slot["expected_abstain"] else parsed["question"]
            train_user = (f"source_id: {slot['source_id']}\n课程: {slot['course']}\n"
                          f"材料：\n{slot['context']}\n\n问题：{question}")
            assistant = {k: parsed[k] for k in ("answer", "abstain", "citations")}
            row["training_sample"] = {
                "messages": [{"role": "system", "content": QA_SYSTEM},
                             {"role": "user", "content": train_user},
                             {"role": "assistant", "content": json.dumps(assistant, ensure_ascii=False)}],
                "course": slot["course"], "intent": "grounded_qa", "synthetic_pilot": True,
                "source_id": slot["source_id"],
            }
        else:
            row["training_sample"] = None


def _write_accepted(rows: list[dict]) -> list[dict]:
    accepted = []
    seen = set()
    for row in rows:
        sample = row.get("training_sample")
        if not row.get("accepted") or not sample:
            continue
        h = _sha(sample["messages"])
        if h in seen:
            continue
        seen.add(h)
        accepted.append({**sample, "candidate_id": row["candidate_id"], "sample_hash": h})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with ACCEPTED_FILE.open("w", encoding="utf-8") as f:
        for row in accepted:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return accepted


def _report(rows: list[dict]) -> dict:
    practice_rows = [r for r in rows if r.get("task") == "practice"]
    _revalidate_solve(rows, practice_rows)
    _revalidate_qa(rows)
    selected_rows = _selected_pilot_rows(rows)
    calls = [c for r in rows for c in r.get("calls", [])]
    accepted = _write_accepted(selected_rows)
    by_task = {}
    by_task_course = {}
    reject_reasons = {}
    for task, target in TARGETS.items():
        rr = [r for r in selected_rows if r["task"] == task]
        by_task[task] = {"completed": len(rr), "target": target,
                         "accepted": sum(r["accepted"] for r in rr),
                         "accept_rate": _pct(sum(r["accepted"] for r in rr), len(rr))}
        by_task_course[task] = {}
        for course in COURSES:
            cc = [r for r in rr if r["course"] == course]
            by_task_course[task][course] = {
                "completed": len(cc), "accepted": sum(r["accepted"] for r in cc),
                "accept_rate": _pct(sum(r["accepted"] for r in cc), len(cc)),
            }
        reject_reasons[task] = dict(Counter(reason for r in rr for reason in r["reject_reasons"]))
    api_success = _pct(sum(not c["error"] for c in calls), len(calls))
    schema_rate = _pct(sum(r.get("schema_ok", False) for r in selected_rows), len(selected_rows))
    retention = _pct(len(accepted), len(selected_rows))
    hashes = [r["sample_hash"] for r in accepted]
    duplicate_rate = _pct(len(hashes) - len(set(hashes)), len(hashes))
    cost = 0.0
    by_model_cost = defaultdict(float)
    for call in calls:
        pi, po = PRICES[call["model"]]
        usage = call["usage"]
        value = (usage["prompt_tokens"] * pi + usage["completion_tokens"] * po) / 1_000_000
        cost += value; by_model_cost[call["model"]] += value
    auto_checks = {
        "complete": len(selected_rows) == sum(TARGETS.values()),
        "api_success": api_success >= GATE["api_success_rate_min"],
        "schema": schema_rate >= GATE["schema_rate_min"],
        "retention": retention >= GATE["retention_rate_min"],
        "duplicates": duplicate_rate <= GATE["duplicate_rate_max"],
        **{f"task_{task}": by_task[task]["accept_rate"] >= threshold
           for task, threshold in GATE["task_accept_rate_min"].items()},
    }
    report = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "targets": TARGETS,
        "completed": len(selected_rows), "raw_candidates": len(rows),
        "api_calls": len(calls), "api_success_rate": api_success,
        "schema_rate": schema_rate, "accepted": len(accepted), "retention_rate": retention,
        "duplicate_rate": duplicate_rate, "by_task": by_task,
        "by_task_course": by_task_course, "reject_reasons": reject_reasons,
        "cost_cny": round(cost, 4), "cost_by_model": {k: round(v, 4) for k, v in by_model_cost.items()},
        "latency_p50": round(statistics.median([c["latency_seconds"] for c in calls]), 3) if calls else 0,
        "auto_checks": auto_checks, "automatic_gate_passed": all(auto_checks.values()),
        "manual_gate": {"status": "pending", "review_n": GATE["manual_review_n"],
                        "accept_rate_min": GATE["manual_accept_rate_min"],
                        "severe_errors_max": GATE["manual_severe_errors_max"]},
        "ready_for_bulk": False, "bulk_if_passed": GATE["bulk_first_accepted"],
        "if_failed": "stop; fix failed task pipelines; rerun a separate 200-case targeted pilot",
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# CourseLoop 500 条合成数据小试", "",
             f"- 完成：{len(selected_rows)}/500；原始候选：{len(rows)}；API 请求：{len(calls)}；API 成功：{api_success}%",
             f"- 接受：{len(accepted)}；保留率：{retention}%；Schema：{schema_rate}%；重复：{duplicate_rate}%",
             f"- 成本：¥{round(cost, 4)}；自动门禁：{'PASS' if report['automatic_gate_passed'] else 'FAIL'}",
             "", "| 任务 | 完成 | 接受 | 通过率 |", "|---|---:|---:|---:|"]
    for task, stat in by_task.items():
        lines.append(f"| {task} | {stat['completed']}/{stat['target']} | {stat['accepted']} | {stat['accept_rate']}% |")
    lines += ["", "## 按课程通过率", "",
              "| 任务 | co | ds-algo | net | os |", "|---|---:|---:|---:|---:|"]
    for task, stats in by_task_course.items():
        lines.append(f"| {task} | " + " | ".join(f"{stats[c]['accept_rate']}%" for c in COURSES) + " |")
    lines += ["", "自动门禁通过后仍须完成 50 条抽样复核（接受率≥90%、严重错误=0），"
              "才可生成首批 3,000 条合格数据。"]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _make_review(accepted)
    return report


def _make_review(accepted: list[dict]) -> None:
    rng = random.Random(20260822)
    chosen = []
    for task in TARGETS:
        rr = [r for r in accepted if r["intent"] == task]
        rng.shuffle(rr)
        chosen.extend(rr[:12])
    rest = [r for r in accepted if r not in chosen]
    rng.shuffle(rest)
    chosen = (chosen + rest)[:GATE["manual_review_n"]]
    with REVIEW_FILE.open("w", encoding="utf-8") as f:
        for row in chosen:
            f.write(json.dumps({"candidate_id": row["candidate_id"], "course": row["course"],
                                "intent": row["intent"], "messages": row["messages"],
                                "review": {"accept": None, "severe_error": None, "notes": ""}},
                               ensure_ascii=False) + "\n")


def run(workers: int) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not MANIFEST_FILE.exists():
        MANIFEST_FILE.write_text(json.dumps({"version": 1, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                             "models": {"practice": QWEN, "diagnose": KIMI,
                                                        "grounded_qa": KIMI, "solve": [KIMI, QWEN]},
                                             "targets": TARGETS, "gate": GATE,
                                             "policy": "isolated pilot; never merge before gate"},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
    practice = _run_stage("practice", _slots(150, "practice"), _practice, workers)
    solve_slots, diagnose_slots, seeds = _dependent_slots(practice)
    existing_solve = _jsonl(RESULTS_DIR / "solve.jsonl")
    legacy_solve = any(re.fullmatch(r"solve-\d+", r.get("candidate_id", "")) for r in existing_solve)
    if legacy_solve:
        solve = _balance_legacy_solve(existing_solve, practice, seeds, workers)
    else:
        solve = _run_stage("solve", solve_slots, lambda s: _solve(s, seeds[s["seed_id"]]), workers)
    diagnose = _run_stage("diagnose", diagnose_slots, lambda s: _diagnose(s, seeds[s["seed_id"]]), workers)
    qa = _run_stage("grounded_qa", _qa_slots(), _qa, workers)
    return _report(practice + solve + diagnose + qa)


def main() -> None:
    ap = argparse.ArgumentParser(description="CourseLoop 500 条合成数据小试")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    if args.report_only:
        rows = []
        for task in TARGETS:
            rows.extend(_jsonl(RESULTS_DIR / f"{task}.jsonl"))
        report = _report(rows)
    else:
        report = run(max(1, args.workers))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
