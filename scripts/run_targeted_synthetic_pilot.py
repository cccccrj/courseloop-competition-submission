"""CourseLoop 合成链路 200 条定向复测。

针对 500 条小试暴露的四个瓶颈做隔离验证：

* 60 条数据结构代码诊断：先对已验证参考代码做确定性 AST 变异，并用测试证明错误；
* 50 条 RAG 问答：先锁定材料原文证据，再让教师生成问题和答案；
* 50 条操作系统选择题：Kimi/Qwen 同题求解，二者都必须命中规范答案；
* 40 条 Schema 回归：四类任务各 10 条，失败时只允许一次格式重试。

所有文件写到 ``data/synthetic_targeted_200``，按条追加、可断点续跑，绝不
覆盖 500 条小试或旧 SFT 数据。只有本脚本的自动门禁通过后，后续放量脚本
才可消费 ``accepted.jsonl``。
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backend.agents.prompts import build_diagnose_system, build_solve_system
from backend.core.courses import get_course
from backend.learning.error_taxonomy import ERROR_CODES, normalize
from backend.verification.unit_test_runner import run_python_tests
from scripts.generate_sft_pilot import (
    COURSES,
    KIMI,
    PRICES,
    QA_SYSTEM,
    QWEN,
    _append,
    _call,
    _evidence_overlap,
    _jsonl,
    _practice,
    _qa_slots,
    _record,
    _sha,
    _verify_noncode_solution,
)
from scripts.test_lora import _extract_json


OUT_DIR = HERE / "data" / "synthetic_targeted_200"
RESULTS_DIR = OUT_DIR / "candidates"
REPORT_JSON = OUT_DIR / "report.json"
REPORT_MD = OUT_DIR / "report.md"
ACCEPTED_FILE = OUT_DIR / "accepted.jsonl"
REVIEW_FILE = OUT_DIR / "review_50.jsonl"
MANIFEST_FILE = OUT_DIR / "manifest.json"

TARGETS = {"diagnose": 60, "grounded_qa": 50, "solve": 50, "schema_regression": 40}
GATE = {
    "api_success_rate_min": 99.0,
    "schema_rate_min": 98.0,
    "duplicate_rate_max": 2.0,
    "task_accept_rate_min": {
        "diagnose": 80.0,
        "grounded_qa": 90.0,
        "solve": 85.0,
        "schema_regression": 98.0,
    },
    "review_n": 50,
}


def _pct(n: float, d: int) -> float:
    return round(100 * n / d, 2) if d else 0.0


def _validated_call(
    model: str,
    messages: list[dict],
    required: set[str],
    temperature: float,
    max_tokens: int,
    extra_check: Callable[[dict], bool] | None = None,
) -> tuple[list[dict], Any, bool]:
    """调用教师模型；仅格式不完整时追加一次完整重生成。"""
    calls: list[dict] = []
    parsed: Any = None
    valid = False
    for attempt in range(2):
        retry_messages = messages
        if attempt:
            retry_messages = messages + [{
                "role": "user",
                "content": (
                    "上一份回复不是完整合法 JSON。请从头重新输出一个完整 JSON 对象；"
                    f"必须包含字段：{', '.join(sorted(required))}。不要解释、不要 Markdown。"
                ),
            }]
        call = _call(model, retry_messages, temperature if not attempt else 0.1, max_tokens)
        calls.append(call)
        parsed = _extract_json(call["raw_output"])
        valid = isinstance(parsed, dict) and required.issubset(parsed)
        if valid and (extra_check is None or extra_check(parsed)):
            return calls, parsed, True
        if call["error"]:
            break
    return calls, parsed, False


def _practice_rows(course: str) -> list[dict]:
    """加载旧数据中可确定性验证的 practice，统一成带 parsed 的种子。"""
    rows: list[dict] = []
    pilot = HERE / "data" / "synthetic_pilot_500" / "candidates" / "practice.jsonl"
    for row in _jsonl(pilot):
        if row.get("course") == course and row.get("accepted") and isinstance(row.get("parsed"), dict):
            rows.append({"candidate_id": row["candidate_id"], "course": course,
                         "topic": row.get("topic", ""), "parsed": row["parsed"]})

    clean = HERE / "data" / "sft" / course / "practice.clean.jsonl"
    for line in clean.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        source = json.loads(line)
        messages = source.get("messages") or []
        parsed = _extract_json(messages[-1].get("content", "")) if messages else None
        if not isinstance(parsed, dict):
            continue
        rows.append({"candidate_id": f"legacy-{_sha(parsed)[:16]}", "course": course,
                     "topic": (parsed.get("knowledge_points") or [""])[0], "parsed": parsed})

    unique: list[dict] = []
    seen = set()
    for row in rows:
        key = _sha(row["parsed"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


_COMPARE_FLIPS = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In,
}
_BINOP_FLIPS = {
    ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Add,
    ast.FloorDiv: ast.Mult, ast.Mod: ast.FloorDiv,
}


class _SingleMutation(ast.NodeTransformer):
    def __init__(self, node_id: int, kind: str):
        self.node_id = node_id
        self.kind = kind

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if getattr(node, "_targeted_id", -1) == self.node_id and self.kind == "compare":
            op = node.ops[0]
            replacement = _COMPARE_FLIPS.get(type(op))
            if replacement:
                node.ops[0] = replacement()
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if getattr(node, "_targeted_id", -1) == self.node_id and self.kind == "binop":
            replacement = _BINOP_FLIPS.get(type(node.op))
            if replacement:
                node.op = replacement()
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if (getattr(node, "_targeted_id", -1) == self.node_id and self.kind == "constant"
                and isinstance(node.value, int) and not isinstance(node.value, bool)):
            node.value = node.value + 1 if node.value >= 0 else node.value - 1
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        if getattr(node, "_targeted_id", -1) == self.node_id and self.kind == "return_none":
            node.value = ast.Constant(value=None)
        return node


def _mutants(problem: dict, limit: int = 3) -> list[tuple[str, str, str]]:
    """返回已由原测试证明失败的 (code, error_tag, mutation)。"""
    code = str(problem.get("code") or "")
    func = str(problem.get("func_name") or "")
    tests = problem.get("test_cases") or []
    if not code or not func or len(tests) < 3 or run_python_tests(code, func, tests).passed is not True:
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    specs: list[tuple[int, str, str, str]] = []
    for node_id, node in enumerate(ast.walk(tree)):
        setattr(node, "_targeted_id", node_id)
        if isinstance(node, ast.Compare) and node.ops and type(node.ops[0]) in _COMPARE_FLIPS:
            specs.append((node_id, "compare", "boundary", "比较边界翻转"))
        elif isinstance(node, ast.BinOp) and type(node.op) in _BINOP_FLIPS:
            specs.append((node_id, "binop", "calculation", "算术运算符替换"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            specs.append((node_id, "constant", "boundary", "边界常量偏移"))
        elif isinstance(node, ast.Return) and node.value is not None:
            specs.append((node_id, "return_none", "reasoning", "返回值逻辑破坏"))
    specs.sort(key=lambda x: _sha([code, x[0], x[1]]))
    out: list[tuple[str, str, str]] = []
    seen = set()
    for node_id, kind, tag, description in specs:
        changed = _SingleMutation(node_id, kind).visit(copy.deepcopy(tree))
        ast.fix_missing_locations(changed)
        mutated = ast.unparse(changed) + "\n"
        if mutated == code or mutated in seen:
            continue
        seen.add(mutated)
        result = run_python_tests(mutated, func, tests)
        if result.passed is False:
            out.append((mutated, tag, description))
            if len(out) >= limit:
                break
    return out


def _diagnose_slots(n: int) -> list[dict]:
    candidates = []
    for seed in _practice_rows("ds-algo"):
        for wrong_code, error_type, mutation in _mutants(seed["parsed"], limit=3):
            candidates.append({
                "candidate_id": f"tdiag-{_sha([seed['candidate_id'], wrong_code])[:18]}",
                "course": "ds-algo", "topic": seed.get("topic", ""),
                "source_candidate_id": seed["candidate_id"], "problem": seed["parsed"],
                "wrong_code": wrong_code, "error_type": error_type, "mutation": mutation,
            })
        # 每个参考实现最多贡献 3 个变异；达到带余量的目标后立即停止，避免为
        # 数百个历史实现重复启动沙箱。余量供 Schema 回归使用。
        if len(candidates) >= n:
            break
    candidates.sort(key=lambda row: _sha(row["candidate_id"]))
    if len(candidates) < n:
        raise RuntimeError(f"可验证代码变异仅 {len(candidates)} < {n}")
    return candidates[:n]


def _diagnose_fixed(slot: dict) -> dict:
    problem = slot["problem"]
    error_type = slot["error_type"]
    required = {"correct", "score", "first_wrong_step", "error_tags",
                "diagnosis", "remediation", "reexplain"}
    messages = [
        {"role": "system", "content": (
            "你是数据结构代码错因诊断助手。错误代码已由单元测试证明失败，不要重写或生成代码。"
            "只输出完整合法 JSON，字段为 correct、score、first_wrong_step、error_tags、"
            "diagnosis、remediation、reexplain。correct 必须为 false，first_wrong_step 必须为 1，"
            f"error_tags 必须严格等于 [\"{error_type}\"]。"
        )},
        {"role": "user", "content": (
            f"题目：{problem['stem']}\n\n参考实现：\n{problem['code']}\n\n"
            f"学生第1步提交的错误代码（变异类型：{slot['mutation']}）：\n{slot['wrong_code']}\n\n"
            "请定位这个已确认存在的错误，并给出最小纠正建议。"
        )},
    ]
    calls, parsed, schema_ok = _validated_call(KIMI, messages, required, 0.2, 1800)
    reasons = []
    if not schema_ok:
        reasons.append("schema_or_json")
    if schema_ok:
        tags = [normalize(str(x)) for x in (parsed.get("error_tags") or [])]
        if tags != [error_type] or any(tag not in ERROR_CODES for tag in tags):
            reasons.append("wrong_error_tag")
        if parsed.get("correct") is not False or parsed.get("first_wrong_step") != 1:
            reasons.append("invalid_diagnosis_label")
    if any(call["error"] for call in calls):
        reasons.append("api_error")
    accepted = not reasons
    sample = None
    if accepted:
        train_user = (f"题目：{problem['stem']}\n\n参考答案：{problem.get('answer', '')}\n\n"
                      f"学生作答：第1步提交代码\n{slot['wrong_code']}\n\n请定位第一个错误步骤并诊断错因：")
        assistant = {key: parsed[key] for key in required}
        sample = {
            "messages": [
                {"role": "system", "content": build_diagnose_system(get_course("ds-algo"))},
                {"role": "user", "content": train_user},
                {"role": "assistant", "content": json.dumps(assistant, ensure_ascii=False)},
            ],
            "course": "ds-algo", "intent": "diagnose", "synthetic_targeted": True,
            "source_candidate_id": slot["source_candidate_id"],
            "verified_mutation": slot["mutation"],
        }
    return _record(slot, "diagnose", calls, parsed, schema_ok, accepted, reasons, sample,
                   drop=("problem", "wrong_code"))


def _locked_evidence(context: str, salt: str) -> str:
    pieces = [match.group(0).strip() for match in
              re.finditer(r"[^。！？!?\n]{28,220}[。！？!?]?", context)]
    pieces = [piece for piece in pieces if len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", piece)) >= 24]
    if not pieces:
        start = int(_sha(salt)[:8], 16) % max(1, len(context) - 180)
        return context[start:start + 180]
    pieces.sort(key=lambda piece: _sha([salt, piece]))
    return pieces[0]


def _targeted_qa_slots(n: int, offset: int = 0) -> list[dict]:
    all_slots = _qa_slots()
    by_course = {course: [row for row in all_slots if row["course"] == course] for course in COURSES}
    counts = [n // 4 + (1 if i < n % 4 else 0) for i in range(4)]
    out = []
    for course, count in zip(COURSES, counts):
        available = by_course[course][offset:offset + count]
        if len(available) < count:
            raise RuntimeError(f"{course} QA slot 不足：{len(available)} < {count}")
        for row in available:
            item = dict(row)
            item["candidate_id"] = f"tqa-{course}-{offset:02d}-{row['candidate_id'].rsplit('-', 1)[-1]}"
            if not item["expected_abstain"]:
                item["locked_evidence"] = _locked_evidence(item["context"], item["candidate_id"])
            out.append(item)
    return out


def _qa_locked(slot: dict) -> dict:
    required = {"question", "answer", "abstain", "citations"}
    if slot["expected_abstain"]:
        task = (f"固定问题：{slot['question']}\n材料不支持该问题。必须 abstain=true、answer 简要说明材料不足、"
                "citations=[]。")
    else:
        task = (
            "已锁定下面这段材料原文作为证据。请生成一个具体问题并仅依据它回答；"
            "citations 中 evidence 必须逐字复制锁定证据，不得改写。\n"
            f"锁定证据：{slot['locked_evidence']}"
        )
    messages = [
        {"role": "system", "content": QA_SYSTEM +
         '\n只输出完整合法JSON：{"question":"","answer":"","abstain":false,'
         '"citations":[{"source_id":"","evidence":""}]}。'},
        {"role": "user", "content":
         f"source_id: {slot['source_id']}\n课程: {slot['course']}\n材料：\n{slot['context']}\n\n{task}"},
    ]
    calls, parsed, schema_ok = _validated_call(KIMI, messages, required, 0.25, 1800)
    reasons = []
    assembled = copy.deepcopy(parsed) if isinstance(parsed, dict) else parsed
    if not schema_ok:
        reasons.append("schema_or_json")
    elif slot["expected_abstain"]:
        if parsed.get("abstain") is not True:
            reasons.append("bad_abstention")
        else:
            assembled["citations"] = []
    else:
        evidence = slot["locked_evidence"]
        answer = str(parsed.get("answer") or "")
        question = str(parsed.get("question") or "")
        if not question or not answer or (len(answer) > 20 and _evidence_overlap(answer, evidence) < 0.05):
            reasons.append("answer_not_supported_by_locked_evidence")
        else:
            assembled["abstain"] = False
            assembled["citations"] = [{"source_id": slot["source_id"], "evidence": evidence}]
    if any(call["error"] for call in calls):
        reasons.append("api_error")
    accepted = not reasons
    sample = None
    if accepted:
        question = slot["question"] if slot["expected_abstain"] else assembled["question"]
        train_user = (f"source_id: {slot['source_id']}\n课程: {slot['course']}\n"
                      f"材料：\n{slot['context']}\n\n问题：{question}")
        assistant = {key: assembled[key] for key in ("answer", "abstain", "citations")}
        sample = {
            "messages": [{"role": "system", "content": QA_SYSTEM},
                         {"role": "user", "content": train_user},
                         {"role": "assistant", "content": json.dumps(assistant, ensure_ascii=False)}],
            "course": slot["course"], "intent": "grounded_qa", "synthetic_targeted": True,
            "source_id": slot["source_id"], "evidence_locked_before_generation": True,
        }
    return _record(slot, "grounded_qa", calls, assembled, schema_ok, accepted, reasons, sample,
                   drop=("context", "locked_evidence"))


def _os_solve_slots(n: int, offset: int = 0) -> list[dict]:
    seeds = []
    for row in _practice_rows("os"):
        problem = row["parsed"]
        canonical = problem.get("final_answer_canonical")
        stem = str(problem.get("stem") or "")
        if not isinstance(canonical, dict) or canonical.get("kind") != "choice":
            continue
        if not all(re.search(rf"(?m)(^|\s){letter}[.、:：]", stem) for letter in "ABCD"):
            continue
        seeds.append(row)
    seeds.sort(key=lambda row: _sha(row["candidate_id"]))
    chosen = seeds[offset:offset + n]
    if len(chosen) < n:
        raise RuntimeError(f"OS 可验证选择题仅 {len(chosen)} < {n}")
    return [{"candidate_id": f"tosolve-{offset+i+1:03d}", "course": "os",
             "source_candidate_id": row["candidate_id"], "problem": row["parsed"]}
            for i, row in enumerate(chosen)]


def _choice_output_ok(parsed: dict) -> bool:
    canonical = parsed.get("final_answer_canonical")
    return (isinstance(canonical, dict) and canonical.get("kind") == "choice"
            and bool(re.match(r"^\s*(?:答案\s*[:：]\s*)?[A-D](?:\b|[.、，,])",
                              str(parsed.get("final_answer") or ""), re.I)))


def _solve_os(slot: dict) -> dict:
    problem = slot["problem"]
    required = {"problem_type", "knowledge_points", "givens", "target", "strategy",
                "steps", "final_answer", "common_errors", "final_answer_canonical"}
    messages = [
        {"role": "system", "content": build_solve_system(get_course("os")) + (
            "\n只输出一个完整合法 JSON，不要 Markdown。此题为单项选择题；final_answer 必须以‘答案：X’开头，"
            "X 只能是 A-D；final_answer_canonical 必须为 {\"kind\":\"choice\",\"value\":\"X\"}。"
            "先独立计算，不要猜测。"
        )},
        {"role": "user", "content": f"题目：{problem['stem']}\n\n请按要求解题。"},
    ]
    calls = []
    parsed_by_model = {}
    schema_by_model = {}
    passes = {}
    details = {}
    for model in (KIMI, QWEN):
        model_calls, parsed, schema_ok = _validated_call(
            model, messages, required, 0.15, 4200, extra_check=_choice_output_ok)
        calls.extend(model_calls)
        parsed_by_model[model] = parsed
        schema_by_model[model] = schema_ok
        passed, detail = _verify_noncode_solution(parsed, problem)
        predicted = parsed.get("final_answer_canonical") if isinstance(parsed, dict) else None
        gold = problem["final_answer_canonical"]
        canonical_same = (isinstance(predicted, dict) and predicted.get("kind") == "choice"
                          and str(predicted.get("value", "")).upper() == str(gold.get("value", "")).upper())
        passes[model] = passed and canonical_same
        details[model] = detail
    schema_ok = all(schema_by_model.values())
    reasons = []
    if not schema_ok:
        reasons.append("schema_or_json")
    if not all(passes.values()):
        reasons.append("teacher_disagreement_or_wrong")
    if any(call["error"] for call in calls):
        reasons.append("api_error")
    accepted = not reasons
    sample = None
    if accepted:
        selected = parsed_by_model[KIMI]
        sample = {
            "messages": messages + [{"role": "assistant", "content": json.dumps(selected, ensure_ascii=False)}],
            "course": "os", "intent": "solve", "synthetic_targeted": True,
            "verified_by": [KIMI, QWEN], "source_candidate_id": slot["source_candidate_id"],
        }
    row = _record(slot, "solve", calls, parsed_by_model.get(KIMI), schema_ok,
                  accepted, reasons, sample, drop=("problem",))
    row["teacher_pass"] = passes
    row["teacher_details"] = details
    row["schema_by_model"] = schema_by_model
    return row


def _schema_row(row: dict, source_task: str) -> dict:
    row = copy.deepcopy(row)
    row["candidate_id"] = f"schema-{source_task}-{row['candidate_id']}"
    row["source_task"] = source_task
    row["task"] = "schema_regression"
    row["training_sample"] = None
    reasons = []
    if not row.get("schema_ok"):
        reasons.append("schema_or_json")
    if any(call.get("error") for call in row.get("calls", [])):
        reasons.append("api_error")
    row["accepted"] = not reasons
    row["reject_reasons"] = reasons
    return row


def _run_stage(name: str, slots: list[dict], fn: Callable[[dict], dict], workers: int) -> list[dict]:
    import concurrent.futures

    path = RESULTS_DIR / f"{name}.jsonl"
    existing = _jsonl(path)
    done = {row["candidate_id"] for row in existing}
    pending = [slot for slot in slots if slot["candidate_id"] not in done]
    print(f"[{name}] 已有 {len(existing)}，待跑 {len(pending)} / {len(slots)}", flush=True)
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fn, slot): slot for slot in pending}
            for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                slot = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001
                    row = _record(slot, name, [], None, False, False,
                                  [f"worker:{type(exc).__name__}:{str(exc)[:300]}"], None,
                                  drop=("problem", "wrong_code", "context", "locked_evidence"))
                _append(path, row)
                print(f"\r[{name}] {i}/{len(pending)} accepted={row['accepted']}   ", end="", flush=True)
        print()
    return _jsonl(path)


def _write_outputs(rows: list[dict]) -> tuple[list[dict], dict]:
    accepted = []
    seen = set()
    for row in rows:
        sample = row.get("training_sample")
        if not row.get("accepted") or not sample:
            continue
        digest = _sha(sample["messages"])
        if digest in seen:
            continue
        seen.add(digest)
        accepted.append({**sample, "candidate_id": row["candidate_id"], "sample_hash": digest})
    with ACCEPTED_FILE.open("w", encoding="utf-8") as handle:
        for row in accepted:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    calls = [call for row in rows for call in row.get("calls", [])]
    by_task = {}
    rejects = {}
    for task, target in TARGETS.items():
        subset = [row for row in rows if row.get("task") == task]
        count = sum(bool(row.get("accepted")) for row in subset)
        by_task[task] = {"completed": len(subset), "target": target,
                         "accepted": count, "accept_rate": _pct(count, len(subset))}
        rejects[task] = dict(Counter(reason for row in subset for reason in row.get("reject_reasons", [])))
    api_success = _pct(sum(not call.get("error") for call in calls), len(calls))
    schema_rate = _pct(sum(bool(row.get("schema_ok")) for row in rows), len(rows))
    hashes = [row["sample_hash"] for row in accepted]
    duplicate_rate = _pct(len(hashes) - len(set(hashes)), len(hashes))
    by_model_cost = defaultdict(float)
    for call in calls:
        in_price, out_price = PRICES[call["model"]]
        usage = call.get("usage") or {}
        by_model_cost[call["model"]] += (
            usage.get("prompt_tokens", 0) * in_price + usage.get("completion_tokens", 0) * out_price
        ) / 1_000_000
    checks = {
        "complete": len(rows) == sum(TARGETS.values()),
        "api_success": api_success >= GATE["api_success_rate_min"],
        "schema": schema_rate >= GATE["schema_rate_min"],
        "duplicates": duplicate_rate <= GATE["duplicate_rate_max"],
        **{f"task_{task}": by_task[task]["accept_rate"] >= threshold
           for task, threshold in GATE["task_accept_rate_min"].items()},
    }
    report = {
        "created_at": time.strftime("%F %T"), "targets": TARGETS, "completed": len(rows),
        "api_calls": len(calls), "api_success_rate": api_success, "schema_rate": schema_rate,
        "accepted_training_samples": len(accepted), "duplicate_rate": duplicate_rate,
        "by_task": by_task, "reject_reasons": rejects,
        "cost_cny": round(sum(by_model_cost.values()), 4),
        "cost_by_model": {key: round(value, 4) for key, value in by_model_cost.items()},
        "latency_p50": round(statistics.median([call["latency_seconds"] for call in calls]), 3) if calls else 0,
        "checks": checks, "automatic_gate_passed": all(checks.values()),
        "next_if_passed": "blind 50-sample dual-model audit, then 3000 accepted bulk generation",
        "next_if_failed": "inspect failed subgate and rerun only a new versioned targeted set",
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# CourseLoop 200 条定向复测", "",
             f"- 自动门禁：{'PASS' if report['automatic_gate_passed'] else 'FAIL'}",
             f"- 完成：{len(rows)}/200；API 请求：{len(calls)}；成功率：{api_success}%",
             f"- Schema：{schema_rate}%；训练样本：{len(accepted)}；成本：¥{report['cost_cny']}", "",
             "| 任务 | 完成 | 接受 | 通过率 |", "|---|---:|---:|---:|"]
    for task, value in by_task.items():
        lines.append(f"| {task} | {value['completed']}/{value['target']} | {value['accepted']} | {value['accept_rate']}% |")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rng = random.Random(20260822)
    review = accepted[:]
    rng.shuffle(review)
    with REVIEW_FILE.open("w", encoding="utf-8") as handle:
        for row in review[:GATE["review_n"]]:
            handle.write(json.dumps({"candidate_id": row["candidate_id"], "course": row["course"],
                                     "intent": row["intent"], "messages": row["messages"],
                                     "audit": {"accept": None, "severe_error": None, "notes": ""}},
                                    ensure_ascii=False) + "\n")
    return accepted, report


def _schema_slots(diag_slots: list[dict]) -> list[tuple[str, dict]]:
    practice = []
    counts = [3, 3, 2, 2]
    for course, count in zip(COURSES, counts):
        topics = get_course(course).topics
        for i in range(count):
            practice.append({"candidate_id": f"spractice-{course}-{i+1:02d}", "course": course,
                             "topic": topics[(i * 5 + 1) % len(topics)], "difficulty": 1 + i % 4})
    return ([('practice', row) for row in practice]
            + [('diagnose', {**row, "candidate_id": f"s{row['candidate_id']}"}) for row in diag_slots[60:70]]
            + [('grounded_qa', row) for row in _targeted_qa_slots(10, offset=13)]
            + [('solve', row) for row in _os_solve_slots(10, offset=50)])


def run(workers: int) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if not MANIFEST_FILE.exists():
        MANIFEST_FILE.write_text(json.dumps({
            "version": 1, "created_at": time.strftime("%F %T"), "targets": TARGETS, "gate": GATE,
            "models": {"diagnose": KIMI, "grounded_qa": KIMI, "solve": [KIMI, QWEN]},
            "policy": "isolated targeted pilot; deterministic evidence/mutations; resumable",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    diagnostic_slots = _diagnose_slots(70)
    diagnose = _run_stage("diagnose", diagnostic_slots[:60], _diagnose_fixed, workers)
    qa = _run_stage("grounded_qa", _targeted_qa_slots(50), _qa_locked, workers)
    solve = _run_stage("solve", _os_solve_slots(50), _solve_os, workers)

    schema_pairs = _schema_slots(diagnostic_slots)
    schema_slots = [{**slot, "candidate_id": f"schema-slot-{task}-{slot['candidate_id']}",
                     "_schema_source_task": task} for task, slot in schema_pairs]

    def schema_fn(slot: dict) -> dict:
        task = slot["_schema_source_task"]
        raw_slot = {key: value for key, value in slot.items() if key != "_schema_source_task"}
        if task == "practice":
            row = _practice(raw_slot)
        elif task == "diagnose":
            row = _diagnose_fixed(raw_slot)
        elif task == "grounded_qa":
            row = _qa_locked(raw_slot)
        else:
            row = _solve_os(raw_slot)
        row["candidate_id"] = slot["candidate_id"]
        return _schema_row(row, task)

    schema = _run_stage("schema_regression", schema_slots, schema_fn, workers)
    _, report = _write_outputs(diagnose + qa + solve + schema)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="CourseLoop 200 条定向合成复测")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if args.report_only:
        rows = []
        for task in TARGETS:
            rows.extend(_jsonl(RESULTS_DIR / f"{task}.jsonl"))
        _, report = _write_outputs(rows)
    else:
        report = run(max(1, args.workers))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
