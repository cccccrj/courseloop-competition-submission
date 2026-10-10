"""CourseLoop 首批 3,000 条高门禁合成数据生成器。

仅在 200 条定向复测与 50 条盲审通过后运行。按任务×课程维持固定配额：
practice 300、diagnose 900、grounded QA 900、solve 900。每个 cell 达到
“已接受”配额即停止，候选与 API 调用逐条落盘，可断点续跑。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backend.agents.prompts import build_diagnose_system, build_solve_system  # noqa: E402
from backend.core.config import settings  # noqa: E402
from backend.core.courses import get_course  # noqa: E402
from backend.learning.error_taxonomy import ERROR_CODES, normalize  # noqa: E402
from backend.verification.numeric_checker import check_numeric  # noqa: E402
from backend.verification.unit_test_runner import run_python_tests  # noqa: E402
from scripts.generate_sft_pilot import (  # noqa: E402
    COURSES, KIMI, PRICES, QA_SYSTEM, QWEN, _append, _evidence_overlap, _jsonl,
    _practice, _record, _sha, _verify_noncode_solution,
)
from scripts.run_targeted_synthetic_pilot import (  # noqa: E402
    _diagnose_fixed, _diagnose_slots, _locked_evidence, _practice_rows, _validated_call,
)
from scripts.teacher_bakeoff import _canonical_token, _wrong_token  # noqa: E402


OUT_DIR = HERE / "data" / "synthetic_bulk_3000"
CANDIDATES_DIR = OUT_DIR / "candidates"
ACCEPTED_FILE = OUT_DIR / "accepted.jsonl"
REPORT_FILE = OUT_DIR / "report.json"
REPORT_MD = OUT_DIR / "report.md"
BLIND_REJECTED_IDS = OUT_DIR / "blind_audit_rejected_ids.json"
MANIFEST_FILE = OUT_DIR / "manifest.json"

CELL_TARGETS = {
    **{("practice", course): 75 for course in COURSES},
    **{("diagnose", course): 225 for course in COURSES},
    **{("grounded_qa", course): 225 for course in COURSES},
    **{("solve", course): 225 for course in COURSES},
}
TOTAL_TARGET = sum(CELL_TARGETS.values())
# Legacy networking/OS banks contain a material number of stale or partial
# canonicals.  Do not weaken truth gates; create fresh independent seed problems
# so a low acceptance rate cannot exhaust the solve pool.
SUPPLEMENTAL_SOLVE_SEEDS = {"net": 120, "os": 120}
# Full semantic verification can quarantine candidates that passed structural
# checks.  Keep a large lazy slot pool: `_run_to_quota` only calls as many as are
# required, so this adds repair headroom without paying for unused generations.
PRACTICE_SLOT_POOL = 300


def _targeted_exclusions() -> dict[str, set[str]]:
    """读取外部 targeted 集的题源标识，bulk 训练必须在题源级与其隔离。"""
    path = HERE / "data" / "synthetic_targeted_200" / "accepted.jsonl"
    rows = _jsonl(path)
    diagnose_sources = {
        str(row["source_candidate_id"]) for row in rows
        if row.get("intent") == "diagnose" and row.get("source_candidate_id")
    }
    solve_sources = {
        str(row["source_candidate_id"]) for row in rows
        if row.get("intent") == "solve" and row.get("source_candidate_id")
    }
    return {
        "diagnose_candidate_ids": {
            str(row["candidate_id"]) for row in rows
            if row.get("intent") == "diagnose" and row.get("candidate_id")
        },
        "qa_source_ids": {
            str(row["source_id"]) for row in rows
            if row.get("intent") == "grounded_qa" and row.get("source_id")
        },
        "solve_source_candidate_ids": {
            str(row["source_candidate_id"]) for row in rows
            if row.get("intent") == "solve" and row.get("source_candidate_id")
        },
        # 同一原题的其他变异、以及跨 diagnose/solve 复用也视为泄漏。
        "all_source_candidate_ids": diagnose_sources | solve_sources,
    }


def _noncode_diagnose_slots(course: str, n: int,
                            excluded_source_candidate_ids: set[str] | None = None) -> list[dict]:
    excluded_source_candidate_ids = excluded_source_candidate_ids or set()
    rows = []
    seeds, seen_problems = [], set()
    for seed in _practice_rows(course) + _bulk_practice_rows(course):
        digest = _sha(seed["parsed"])
        if digest in seen_problems:
            continue
        seen_problems.add(digest)
        seeds.append(seed)
    for seed in seeds:
        if str(seed["candidate_id"]) in excluded_source_candidate_ids:
            continue
        canonical = seed["parsed"].get("final_answer_canonical")
        if not isinstance(canonical, dict):
            continue
        wrong, error_type = _wrong_token(canonical)
        if error_type not in ERROR_CODES or check_numeric(wrong, canonical).passed is not False:
            continue
        rows.append({
            "candidate_id": f"bdiag-{course}-{_sha([seed['candidate_id'], wrong])[:18]}",
            "course": course, "topic": seed.get("topic", ""),
            "source_candidate_id": seed["candidate_id"], "problem": seed["parsed"],
            "wrong_answer": wrong, "error_type": error_type,
        })
    rows.sort(key=lambda row: _sha(row["candidate_id"]))
    if len(rows) < 225:
        raise RuntimeError(f"{course} 可验证诊断种子 {len(rows)} < 最低配额 225")
    # 历史 practice 中带 canonical 的唯一题源数量各异；请求的是候选上限，
    # 不应因某课程不足该上限而拒绝其全部可用候选。
    return rows[:min(n, len(rows))]


def _diagnose_noncode(slot: dict) -> dict:
    problem = slot["problem"]
    error_type = slot["error_type"]
    required = {"student_steps", "correct", "score", "first_wrong_step", "error_tags",
                "diagnosis", "remediation", "reexplain"}
    messages = [
        {"role": "system", "content": (
            f"你是{get_course(slot['course']).display}错因诊断数据构造助手。给定的错误最终答案已由机器确认错误。"
            "请构造从第1步开始偏离的简短学生推导，并诊断第一个错误。只输出完整 JSON，字段为 student_steps、"
            "correct、score、first_wrong_step、error_tags、diagnosis、remediation、reexplain。"
            f"correct=false，first_wrong_step=1，error_tags 必须严格等于 [\"{error_type}\"]。"
        )},
        {"role": "user", "content": (
            f"题目：{problem['stem']}\n\n参考答案：{problem.get('answer', '')}\n"
            f"规范答案：{json.dumps(problem['final_answer_canonical'], ensure_ascii=False)}\n"
            f"学生错误最终答案：{slot['wrong_answer']}\n请补出简短错误推导并完成诊断。"
        )},
    ]
    def diagnosis_contract(value: dict) -> bool:
        tags = [normalize(str(item)) for item in (value.get("error_tags") or [])]
        return (value.get("correct") is False and value.get("first_wrong_step") == 1
                and tags == [error_type]
                and isinstance(value.get("student_steps"), list) and bool(value["student_steps"]))

    # 不只对坏 JSON 重试：标签/首错不服从构造约束时也完整重生成一次。
    calls, parsed, contract_ok = _validated_call(
        KIMI, messages, required, 0.35, 2000, extra_check=diagnosis_contract)
    schema_ok = isinstance(parsed, dict) and required.issubset(parsed)
    reasons = []
    if not schema_ok:
        reasons.append("schema_or_json")
    if schema_ok:
        tags = [normalize(str(value)) for value in (parsed.get("error_tags") or [])]
        if tags != [error_type]:
            reasons.append("wrong_error_tag")
        if parsed.get("correct") is not False or parsed.get("first_wrong_step") != 1:
            reasons.append("invalid_diagnosis_label")
        if not isinstance(parsed.get("student_steps"), list) or not parsed["student_steps"]:
            reasons.append("missing_student_steps")
        if not contract_ok and not reasons:
            reasons.append("diagnosis_contract_failed")
    if any(call["error"] for call in calls):
        reasons.append("api_error")
    accepted = not reasons
    sample = None
    if accepted:
        steps = "\n".join(f"第{i + 1}步：{value}" for i, value in enumerate(parsed["student_steps"]))
        student = f"{steps}\n最终答案：{slot['wrong_answer']}"
        train_user = (f"题目：{problem['stem']}\n\n参考答案：{problem.get('answer', '')}\n\n"
                      f"学生作答：{student}\n\n请定位第一个错误步骤并诊断错因：")
        assistant = {key: parsed[key] for key in
                     ("correct", "score", "first_wrong_step", "error_tags", "diagnosis", "remediation", "reexplain")}
        sample = {
            "messages": [{"role": "system", "content": build_diagnose_system(get_course(slot["course"]))},
                         {"role": "user", "content": train_user},
                         {"role": "assistant", "content": json.dumps(assistant, ensure_ascii=False)}],
            "course": slot["course"], "intent": "diagnose", "synthetic_bulk": True,
            "source_candidate_id": slot["source_candidate_id"], "wrong_answer_verified": True,
        }
    return _record(slot, "diagnose", calls, parsed, schema_ok, accepted, reasons, sample,
                   drop=("problem", "wrong_answer"))


def _qa_pool(per_course: int, excluded_source_ids: set[str] | None = None) -> list[dict]:
    import chromadb

    excluded_source_ids = excluded_source_ids or set()
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
        got = collection.get(where={"course": course}, limit=1500, include=["documents", "metadatas"])
        chunks = []
        for cid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"]):
            if str(cid) in excluded_source_ids:
                continue
            context = (doc or "").strip()
            title = str((meta or {}).get("section") or (meta or {}).get("title_path") or
                        (meta or {}).get("chapter") or (meta or {}).get("source") or "")
            if not (450 <= len(context) <= 3000 and title):
                continue
            if re.search(r"目录|致谢|如何使用|作者|Release", title + context[:100]):
                continue
            chunks.append((cid, context[:2200], title))
        chunks.sort(key=lambda value: _sha([course, value[0]]))
        variants = []
        for cid, context, title in chunks:
            for variant in range(3):
                candidate_id = f"bqa-{course}-{_sha([cid, variant])[:18]}"
                abstain = (len(variants) % 20 == 19)
                variants.append({
                    "candidate_id": candidate_id, "course": course, "source_id": cid,
                    "context": context, "title": title, "expected_abstain": abstain,
                    "question": foreign[course] if abstain else "",
                    "locked_evidence": "" if abstain else _locked_evidence(context, candidate_id),
                })
                if len(variants) >= per_course:
                    break
            if len(variants) >= per_course:
                break
        if len(variants) < per_course:
            raise RuntimeError(f"{course} QA 候选 {len(variants)} < {per_course}")
        out.extend(variants)
    return out


def _qa_locked(slot: dict) -> dict:
    required = {"question", "answer", "abstain", "citations"}
    if slot["expected_abstain"]:
        task = f"固定问题：{slot['question']}\n材料不支持时必须 abstain=true、citations=[]。"
    else:
        task = ("围绕锁定证据生成一个具体问题并回答。回答只能使用锁定证据；citation evidence 必须逐字复制。\n"
                f"锁定证据：{slot['locked_evidence']}")
    messages = [
        {"role": "system", "content": QA_SYSTEM +
         '\n只输出完整合法JSON：{"question":"","answer":"","abstain":false,'
         '"citations":[{"source_id":"","evidence":""}]}。'},
        {"role": "user", "content": f"source_id: {slot['source_id']}\n课程: {slot['course']}\n"
         f"材料：\n{slot['context']}\n\n{task}"},
    ]
    calls, parsed, schema_ok = _validated_call(KIMI, messages, required, 0.3, 1800)
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
        answer = str(parsed.get("answer") or "")
        question = str(parsed.get("question") or "")
        evidence = slot["locked_evidence"]
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
        train_user = f"source_id: {slot['source_id']}\n课程: {slot['course']}\n材料：\n{slot['context']}\n\n问题：{question}"
        assistant = {key: assembled[key] for key in ("answer", "abstain", "citations")}
        sample = {
            "messages": [{"role": "system", "content": QA_SYSTEM},
                         {"role": "user", "content": train_user},
                         {"role": "assistant", "content": json.dumps(assistant, ensure_ascii=False)}],
            "course": slot["course"], "intent": "grounded_qa", "synthetic_bulk": True,
            "source_id": slot["source_id"], "evidence_locked_before_generation": True,
        }
    return _record(slot, "grounded_qa", calls, assembled, schema_ok, accepted, reasons, sample,
                   drop=("context", "locked_evidence"))


def _bulk_practice_rows(course: str) -> list[dict]:
    """复用本轮已通过确定性校验的新 practice，扩大 solve 独立候选池。"""
    path = CANDIDATES_DIR / f"practice_{course}.jsonl"
    out = []
    for row in _jsonl(path):
        if row.get("accepted") and isinstance(row.get("parsed"), dict):
            out.append({
                "candidate_id": row["candidate_id"], "course": course,
                "topic": row.get("topic", ""), "parsed": row["parsed"],
            })
    return out


def _supplemental_practice_rows(course: str) -> list[dict]:
    """Fresh seed problems used only as independent sources for solve synthesis."""
    path = CANDIDATES_DIR / f"solve_seed_{course}.jsonl"
    out = []
    for row in _jsonl(path):
        if row.get("accepted") and isinstance(row.get("parsed"), dict):
            out.append({
                "candidate_id": row["candidate_id"], "course": course,
                "topic": row.get("topic", ""), "parsed": row["parsed"],
            })
    return out


def _supplemental_practice_slots(course: str, n: int) -> list[dict]:
    topics = get_course(course).topics
    return [{
        "candidate_id": f"bsolve-seed-{course}-{i + 1:04d}",
        "course": course,
        "topic": topics[(i * 11 + 5) % len(topics)],
        "difficulty": 1 + (i % 4),
    } for i in range(n)]


_OPTION_RE = re.compile(r"(?:^|\s|\n)([A-D])[.、:：]\s*", re.I)
_MULTIPART_RE = re.compile(
    r"(?:请回答以下问题|(?:^|\n)\s*(?:\(?[1-9][.)、]|（[一二三四五六七八九]))",
    re.M,
)


def _canonical_seed_is_usable(problem: dict) -> bool:
    """历史题的 canonical 必须足以唯一核验整题，而不只是格式可解析。"""
    canonical = problem.get("final_answer_canonical")
    if not isinstance(canonical, dict):
        return False
    if check_numeric(_canonical_token(canonical), canonical).passed is not True:
        return False
    stem = str(problem.get("stem") or "")
    kind = canonical.get("kind")
    if kind == "choice":
        gold = canonical.get("value")
        values = gold if isinstance(gold, list) else [gold]
        options = {value.upper() for value in _OPTION_RE.findall(stem)}
        # 没有显式选项却只给字母 gold，无法从题干独立恢复字母含义。
        if not values or any(str(value).strip().upper() not in options for value in values):
            return False
    if kind in {"scalar", "percent", "int", "bits", "ipv4net"} and _MULTIPART_RE.search(stem):
        # 单 token canonical 不能覆盖多问大题，容易把只答其中一问误判为整题正确。
        return False
    return True


def _solve_slots(course: str, n: int,
                 excluded_source_candidate_ids: set[str] | None = None) -> list[dict]:
    excluded_source_candidate_ids = excluded_source_candidate_ids or set()
    rows = []
    seeds, seen_problems = [], set()
    for seed in (_practice_rows(course) + _bulk_practice_rows(course)
                 + _supplemental_practice_rows(course)):
        digest = _sha(seed["parsed"])
        if digest in seen_problems:
            continue
        seen_problems.add(digest)
        seeds.append(seed)
    for seed in seeds:
        if str(seed["candidate_id"]) in excluded_source_candidate_ids:
            continue
        problem = seed["parsed"]
        # 历史 clean 数据里混有伪 canonical 或只覆盖多问大题一小问的残缺
        # canonical。这类题无法确定性判断教师整题答案，禁止消耗 API/进入训练。
        if course != "ds-algo" and not _canonical_seed_is_usable(problem):
            continue
        if course == "ds-algo" and not (problem.get("code") and problem.get("func_name") and problem.get("test_cases")):
            continue
        rows.append({"candidate_id": f"bsolve-{course}-{_sha(seed['candidate_id'])[:18]}",
                     "course": course, "source_candidate_id": seed["candidate_id"], "problem": problem})
    rows.sort(key=lambda row: _sha(row["candidate_id"]))
    if len(rows) < 225:
        raise RuntimeError(f"{course} solve 独立题源 {len(rows)} < 最低配额 225")
    return rows[:min(n, len(rows))]


def _solve(slot: dict) -> dict:
    course, problem = slot["course"], slot["problem"]
    cobj = get_course(course)
    required = {"problem_type", "knowledge_points", "givens", "target", "strategy",
                "steps", "final_answer", "common_errors"}
    if cobj.code_enabled:
        required |= {"code", "func_name", "complexity"}
    else:
        required.add("final_answer_canonical")
    suffix = "只输出一个完整合法 JSON，不要 Markdown。"
    if isinstance(problem.get("final_answer_canonical"), dict) and problem["final_answer_canonical"].get("kind") == "choice":
        suffix += (" 此题为选择题，final_answer 必须以‘答案：X’开头；"
                   "final_answer_canonical 必须为 kind=choice、value=X。")
    messages = [{"role": "system", "content": build_solve_system(cobj) + "\n" + suffix},
                {"role": "user", "content": f"题目：{problem['stem']}\n\n请按要求解题。"}]

    def solution_contract(value: dict) -> bool:
        """让格式正确但答案/规范 token 错误的回复也获得一次完整重生成。"""
        if cobj.code_enabled:
            if not value.get("code") or str(value.get("func_name")) != str(problem["func_name"]):
                return False
            return run_python_tests(
                str(value["code"]), str(value["func_name"]), problem["test_cases"]
            ).passed is True
        passed, _ = _verify_noncode_solution(value, problem)
        predicted = value.get("final_answer_canonical")
        gold = problem["final_answer_canonical"]
        return bool(
            passed
            and isinstance(predicted, dict)
            and predicted.get("kind") == gold.get("kind")
            and check_numeric(_canonical_token(predicted), gold).passed is True
        )

    calls, parsed_by_model, schemas, passes, details = [], {}, {}, {}, {}
    for model in (KIMI, QWEN):
        model_calls, parsed, _contract_ok = _validated_call(
            model, messages, required, 0.15, 4200, extra_check=solution_contract)
        calls.extend(model_calls)
        # _validated_call 的第三个返回值还包含 extra_check（真值契约）的
        # 结果；Schema 指标只能统计 JSON/字段完整性，不能把答错题冒充格式错。
        parsed_by_model[model] = parsed
        schemas[model] = isinstance(parsed, dict) and required.issubset(parsed)
        if cobj.code_enabled:
            if not isinstance(parsed, dict) or not parsed.get("code") or not parsed.get("func_name"):
                passed, detail = False, "missing_code"
            elif str(parsed["func_name"]) != str(problem["func_name"]):
                passed, detail = False, "func_name_changed"
            else:
                result = run_python_tests(str(parsed["code"]), str(parsed["func_name"]), problem["test_cases"])
                passed, detail = result.passed is True, result.details
        else:
            passed, detail = _verify_noncode_solution(parsed, problem)
            predicted = parsed.get("final_answer_canonical") if isinstance(parsed, dict) else None
            gold = problem["final_answer_canonical"]
            if isinstance(predicted, dict) and predicted.get("kind") == gold.get("kind"):
                # 最终文字命中 gold 之外，再要求模型自报 canonical 本身也命中 gold。
                passed = passed and check_numeric(_canonical_token(predicted), gold).passed is True
            else:
                passed = False
        passes[model], details[model] = passed, detail
    schema_ok = all(schemas.values())
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
        sample = {
            "messages": messages + [{"role": "assistant", "content": json.dumps(parsed_by_model[KIMI], ensure_ascii=False)}],
            "course": course, "intent": "solve", "synthetic_bulk": True,
            "verified_by": [KIMI, QWEN], "source_candidate_id": slot["source_candidate_id"],
        }
    row = _record(slot, "solve", calls, parsed_by_model.get(KIMI), schema_ok, accepted, reasons, sample,
                  drop=("problem",))
    row["teacher_pass"], row["teacher_details"], row["schema_by_model"] = passes, details, schemas
    return row


def _practice_slots(course: str, n: int) -> list[dict]:
    topics = get_course(course).topics
    return [{"candidate_id": f"bpractice-{course}-{i + 1:04d}", "course": course,
             "topic": topics[(i * 7 + 3) % len(topics)], "difficulty": 1 + i % 4}
            for i in range(n)]


def _source_tokens(row: dict) -> set[tuple[str, str]]:
    tokens = set()
    if row.get("source_candidate_id"):
        tokens.add(("candidate", str(row["source_candidate_id"])))
    if row.get("source_id"):
        tokens.add(("source", str(row["source_id"])))
    intent = row.get("intent") or row.get("task")
    if intent == "diagnose" and row.get("candidate_id"):
        tokens.add(("diagnose_candidate", str(row["candidate_id"])))
    return tokens


def _targeted_source_overlap(rows: list[dict]) -> set[tuple[str, str]]:
    targeted = _jsonl(HERE / "data" / "synthetic_targeted_200" / "accepted.jsonl")
    target_tokens = set().union(*(_source_tokens(row) for row in targeted)) if targeted else set()
    bulk_tokens = set().union(*(_source_tokens(row) for row in rows)) if rows else set()
    return target_tokens & bulk_tokens


def _blind_rejected_ids() -> set[str]:
    if not BLIND_REJECTED_IDS.exists():
        return set()
    try:
        payload = json.loads(BLIND_REJECTED_IDS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    values = payload.get("candidate_ids", []) if isinstance(payload, dict) else payload
    return {str(value) for value in values}


def _accepted_unique(rows: list[dict], excluded_ids: set[str] | None = None) -> list[dict]:
    """Return deterministic accepted rows unique by the actual training messages."""
    selected: list[dict] = []
    seen: set[str] = set()
    excluded_ids = _blind_rejected_ids() if excluded_ids is None else set(excluded_ids)
    accepted = sorted(
        (row for row in rows
         if row.get("accepted") and row.get("training_sample")
         and str(row.get("candidate_id")) not in excluded_ids),
        key=lambda row: _sha(row["candidate_id"]),
    )
    for row in accepted:
        digest = _sha(row["training_sample"]["messages"])
        if digest in seen:
            continue
        seen.add(digest)
        selected.append(row)
    return selected


def _run_to_quota(name: str, slots: list[dict], fn: Callable[[dict], dict],
                  quota: int, workers: int, batch_size: int = 32) -> list[dict]:
    path = CANDIDATES_DIR / f"{name}.jsonl"
    rows = _jsonl(path)
    done = {row["candidate_id"] for row in rows}
    available = [slot for slot in slots if slot["candidate_id"] not in done]
    unique_count = len(_accepted_unique(rows))
    print(f"[{name}] 已有候选 {len(rows)}，唯一接受 {unique_count}/{quota}", flush=True)
    while unique_count < quota and available:
        shortfall = quota - unique_count
        # Initial fills still use full batches; repair rounds should not pay for
        # 32 calls when only a handful of replacements are missing.  A 1.5x
        # allowance absorbs normal structural rejection and loops again if needed.
        request_n = min(batch_size, len(available), max(4, (shortfall * 3 + 1) // 2))
        batch = available[:request_n]
        available = available[request_n:]
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fn, slot): slot for slot in batch}
            for future in concurrent.futures.as_completed(futures):
                slot = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001
                    row = _record(slot, name, [], None, False, False,
                                  [f"worker:{type(exc).__name__}:{str(exc)[:300]}"], None,
                                  drop=("problem", "wrong_code", "wrong_answer", "context", "locked_evidence"))
                _append(path, row)
                rows.append(row)
        unique_count = len(_accepted_unique(rows))
        print(f"[{name}] 候选 {len(rows)}，唯一接受 {unique_count}/{quota}", flush=True)
    if unique_count < quota:
        raise RuntimeError(f"{name} 候选池耗尽，唯一接受 {unique_count} < {quota}")
    return rows


def _needs_quota(name: str, quota: int) -> bool:
    """Cheap preflight used to avoid rebuilding large source pools for full cells."""
    path = CANDIDATES_DIR / f"{name}.jsonl"
    return len(_accepted_unique(_jsonl(path))) < quota


def _choose_preserving_previous(accepted: list[dict], target: int,
                                selected_hashes: set[str],
                                preferred_ids: list[str]) -> list[dict]:
    """Keep still-valid deployed samples stable, then fill only real vacancies."""
    by_id = {str(row.get("candidate_id")): row for row in accepted}
    ordered = [by_id[candidate_id] for candidate_id in preferred_ids if candidate_id in by_id]
    preferred_set = set(preferred_ids)
    ordered.extend(row for row in accepted if str(row.get("candidate_id")) not in preferred_set)
    chosen = []
    for row in ordered:
        digest = _sha(row["training_sample"]["messages"])
        if digest in selected_hashes:
            continue
        chosen.append(row)
        selected_hashes.add(digest)
        if len(chosen) == target:
            break
    return chosen


def _write_report(all_rows: dict[tuple[str, str], list[dict]]) -> dict:
    previous = _jsonl(ACCEPTED_FILE)
    selected = []
    cell_stats = {}
    all_candidates = []
    for cell, target in CELL_TARGETS.items():
        rows = all_rows[cell]
        all_candidates.extend(rows)
        rejected_ids = _blind_rejected_ids()
        accepted_raw = [row for row in rows
                        if row.get("accepted") and row.get("training_sample")
                        and str(row.get("candidate_id")) not in rejected_ids]
        accepted = _accepted_unique(rows)
        selected_hashes = {
            _sha(row["training_sample"]["messages"]) for row in selected
        }
        preferred_ids = [
            str(row.get("candidate_id")) for row in previous
            if (row.get("intent"), row.get("course")) == cell
        ]
        chosen = _choose_preserving_previous(
            accepted, target, selected_hashes, preferred_ids)
        selected.extend(chosen)
        cell_stats[f"{cell[0]}/{cell[1]}"] = {
            "target_accepted": target, "candidates": len(rows),
            "accepted": len(accepted_raw), "accepted_unique": len(accepted),
            "selected_unique": len(chosen),
            "candidate_accept_rate": round(100 * len(accepted_raw) / len(rows), 2) if rows else 0,
        }
    samples, seen = [], set()
    for row in selected:
        sample = row["training_sample"]
        digest = _sha(sample["messages"])
        if digest in seen:
            continue
        seen.add(digest)
        samples.append({**sample, "candidate_id": row["candidate_id"], "sample_hash": digest})
    with ACCEPTED_FILE.open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")

    supplemental_rows = [
        row for course in SUPPLEMENTAL_SOLVE_SEEDS
        for row in _jsonl(CANDIDATES_DIR / f"solve_seed_{course}.jsonl")
    ]
    quality_candidates = all_candidates + supplemental_rows
    calls = [call for row in quality_candidates for call in row.get("calls", [])]
    api_success = round(100 * sum(not call.get("error") for call in calls) / len(calls), 2) if calls else 0
    schema_rate = round(
        100 * sum(row.get("schema_ok") for row in quality_candidates) / len(quality_candidates), 2
    )
    def call_costs(values: list[dict]) -> defaultdict[str, float]:
        result: defaultdict[str, float] = defaultdict(float)
        for call in values:
            ip, op = PRICES[call["model"]]
            usage = call.get("usage") or {}
            result[call["model"]] += (usage.get("prompt_tokens", 0) * ip
                                      + usage.get("completion_tokens", 0) * op) / 1_000_000
        return result

    costs = call_costs(calls)
    quarantine_rows = [row for path in (OUT_DIR / "quarantine").glob("*.jsonl")
                       for row in _jsonl(path)] if (OUT_DIR / "quarantine").exists() else []
    quarantine_calls = [call for row in quarantine_rows for call in row.get("calls", [])]
    quarantine_costs = call_costs(quarantine_calls)
    actual_cost = sum(costs.values()) + sum(quarantine_costs.values())
    source_overlap = _targeted_source_overlap(selected)
    report = {
        "created_at": time.strftime("%F %T"), "target_accepted": TOTAL_TARGET,
        "accepted_unique": len(samples), "candidate_count": len(quality_candidates), "api_calls": len(calls),
        "supplemental_seed_candidates": len(supplemental_rows),
        "api_success_rate": api_success, "schema_rate": schema_rate, "duplicate_rate": 0.0,
        "cost_cny": round(actual_cost, 4),
        "active_training_cost_cny": round(sum(costs.values()), 4),
        "quarantined_cost_cny": round(sum(quarantine_costs.values()), 4),
        "quarantined_api_calls": len(quarantine_calls),
        "cost_by_model": {key: round(value, 4) for key, value in costs.items()},
        "cells": cell_stats,
        "targeted_source_overlap": len(source_overlap),
        "targeted_source_overlap_preview": [list(value) for value in sorted(source_overlap)[:10]],
        "reject_reasons": dict(Counter(reason for row in quality_candidates
                                       for reason in row.get("reject_reasons", []))),
    }
    report["passed"] = (len(samples) == TOTAL_TARGET and api_success >= 99.0
                        and schema_rate >= 98.0 and not source_overlap)
    report["next"] = "blind audit 50, then prepare incremental SFT" if report["passed"] else "stop and repair failed gate"
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# CourseLoop 3,000 条合成数据放量报告", "",
             f"- 门禁：{'PASS' if report['passed'] else 'FAIL'}",
             f"- 唯一合格样本：{len(samples)}/3000；候选：{len(all_candidates)}；API：{len(calls)}",
             f"- API 成功率：{api_success}%；Schema：{schema_rate}%；成本：¥{report['cost_cny']}", "",
             "| 单元 | 目标 | 候选 | 合格 | 候选通过率 |", "|---|---:|---:|---:|---:|"]
    for cell, stat in cell_stats.items():
        lines.append(f"| {cell} | {stat['target_accepted']} | {stat['candidates']} | {stat['accepted']} | {stat['candidate_accept_rate']}% |")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run(workers: int) -> dict:
    targeted = HERE / "data" / "synthetic_targeted_200" / "report.json"
    audit = HERE / "data" / "synthetic_targeted_200" / "blind_audit_report.json"
    if not targeted.exists() or not json.loads(targeted.read_text(encoding="utf-8")).get("automatic_gate_passed"):
        raise RuntimeError("200 条定向复测尚未通过，禁止放量")
    if not audit.exists() or not json.loads(audit.read_text(encoding="utf-8")).get("passed"):
        raise RuntimeError("50 条盲审尚未通过，禁止放量")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    if not MANIFEST_FILE.exists():
        MANIFEST_FILE.write_text(json.dumps({"version": 1, "created_at": time.strftime("%F %T"),
                                             "cell_targets": {f"{a}/{b}": n for (a, b), n in CELL_TARGETS.items()},
                                             "policy": "accepted quotas; deterministic gates; resumable"},
                                            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows: dict[tuple[str, str], list[dict]] = {}
    exclusions = _targeted_exclusions()
    qa_needed = any(_needs_quota(f"grounded_qa_{course}", 225) for course in COURSES)
    qa_slots = _qa_pool(320, exclusions["qa_source_ids"]) if qa_needed else []
    # targeted 的 60 个确定性变异与 bulk 题源完全隔离；多取余量供拒绝重试。
    ds_diag_needed = _needs_quota("diagnose_ds-algo", 225)
    ds_diag = ([
        row for row in _diagnose_slots(400)
        if row["candidate_id"] not in exclusions["diagnose_candidate_ids"]
        and str(row["source_candidate_id"]) not in exclusions["all_source_candidate_ids"]
    ] if ds_diag_needed else [])
    if ds_diag_needed and len(ds_diag) < 225:
        raise RuntimeError(f"ds-algo 独立诊断题源 {len(ds_diag)} < 最低配额 225")
    for course in COURSES:
        rows[("practice", course)] = _run_to_quota(
            f"practice_{course}", _practice_slots(course, PRACTICE_SLOT_POOL),
            _practice, 75, workers)
    for course in COURSES:
        name = f"diagnose_{course}"
        needed = _needs_quota(name, 225)
        slots = (ds_diag if course == "ds-algo" else (
            _noncode_diagnose_slots(course, 500, exclusions["all_source_candidate_ids"])
            if needed else []))
        fn = _diagnose_fixed if course == "ds-algo" else _diagnose_noncode
        rows[("diagnose", course)] = _run_to_quota(name, slots, fn, 225, workers)
    for course in COURSES:
        slots = [row for row in qa_slots if row["course"] == course]
        rows[("grounded_qa", course)] = _run_to_quota(
            f"grounded_qa_{course}", slots, _qa_locked, 225, workers)
    for course, target in SUPPLEMENTAL_SOLVE_SEEDS.items():
        # Request a larger deterministic slot pool because malformed generations
        # are rejected; only accepted seeds are exposed to the solve teachers.
        _run_to_quota(
            f"solve_seed_{course}", _supplemental_practice_slots(course, target + 80),
            _practice, target, workers,
        )
    for course in COURSES:
        name = f"solve_{course}"
        rows[("solve", course)] = _run_to_quota(
            name,
            # 低质量候选会被双教师和确定性门禁拒绝；使用全部独立题源，
            # 由 accepted quota 停止，避免固定候选上限在低接受率课程上耗尽。
            (_solve_slots(course, 1_000_000, exclusions["all_source_candidate_ids"])
             if _needs_quota(name, 225) else []),
            _solve, 225, workers)
    return _write_report(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="CourseLoop 3,000 条门禁合成数据")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(run(max(1, args.workers)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
