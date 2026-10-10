"""CourseLoop targeted 集语义评测：不仅检查 JSON，还检查任务结果。

targeted 集从未进入增量训练，包含 diagnose / grounded_qa / OS solve。评测规则：
- diagnose：correct、first_wrong_step、error_tags 与确定性 gold 一致；
- grounded_qa：拒答行为正确，或引用 source/evidence 确为用户材料原文且回答受证据支持；
- solve：final_answer / canonical 命中 gold canonical。

预测按条追加，可断点续跑；报告与普通 test_lora 分开保存。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    if int(os.environ.get("OMP_NUM_THREADS", "1")) < 1:
        os.environ["OMP_NUM_THREADS"] = "1"
except ValueError:
    os.environ["OMP_NUM_THREADS"] = "1"

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backend.verification.numeric_checker import check_numeric  # noqa: E402
from backend.agents.prompts import (  # noqa: E402
    build_diagnose_system,
    build_grounded_qa_system,
)
from backend.core.courses import course_ids, get_course  # noqa: E402
from backend.core.generation_contract import production_task_limits  # noqa: E402
from scripts.test_lora import _extract_json, _generate, _load_standalone, _norm_step  # noqa: E402


DEFAULT_DATA = HERE / "data" / "synthetic_targeted_200" / "accepted.jsonl"
EVAL_DIR = HERE / "data" / "eval"
CACHE_VERSION = 3


def _production_prompt_contract_fingerprint() -> str:
    """Bind cached generations to the exact shared prompts used in production."""
    payload = {
        "grounded_qa": build_grounded_qa_system(),
        "diagnose": {
            course_id: build_diagnose_system(get_course(course_id))
            for course_id in course_ids()
        },
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _production_task_limits() -> dict[str, int]:
    return production_task_limits()


def _effective_max_new_tokens(intent: str, fallback: int,
                              refresh_production_prompts: bool) -> int:
    if not refresh_production_prompts:
        return fallback
    return _production_task_limits().get(intent, fallback)


def _semantic_signature(adapter: str, data_path: Path, *, max_seq: int,
                        max_new: int, refresh_production_prompts: bool,
                        excluded_intents: set[str]) -> str:
    adapter_path = Path(adapter)
    if not adapter_path.is_absolute():
        adapter_path = HERE / adapter_path
    fingerprints = []
    for name in ("adapter_config.json", "adapter_model.safetensors"):
        stat = (adapter_path / name).stat()
        fingerprints.append((name, stat.st_size, stat.st_mtime_ns))
    resolved_data = data_path if data_path.is_absolute() else HERE / data_path
    data_stat = resolved_data.stat()
    payload = {
        "version": CACHE_VERSION,
        "adapter": str(adapter_path.resolve()),
        "adapter_files": fingerprints,
        "data": (str(resolved_data.resolve()), data_stat.st_size, data_stat.st_mtime_ns),
        "max_seq": max_seq,
        "max_new": max_new,
        "refresh_production_prompts": refresh_production_prompts,
        "production_prompt_contract": (
            _production_prompt_contract_fingerprint()
            if refresh_production_prompts else None
        ),
        "production_task_max_tokens": (
            _production_task_limits() if refresh_production_prompts else None
        ),
        "excluded_intents": sorted(excluded_intents),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _evidence_overlap(answer: str, evidence: str) -> float:
    """训练环境无 RAG 依赖时也能运行的轻量字面支持度。"""
    def tokens(text: str) -> set[str]:
        text = str(text).lower()
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        grams = {chinese[i:i + 2] for i in range(max(0, len(chinese) - 1))}
        return grams | set(re.findall(r"[a-z]+\d*|\d+(?:\.\d+)?", text))
    left, right = tokens(answer), tokens(evidence)
    return len(left & right) / max(1, len(left))


def _solution_score(pred: dict, canonical: dict, stem: str) -> tuple[bool, str]:
    """核对 solve gold；targeted 当前以 OS choice 为主，同时保留数值型支持。"""
    kind = canonical.get("kind")
    predicted = pred.get("final_answer_canonical")
    if kind == "choice":
        gold = canonical.get("value")
        gold_values = {str(value).strip().upper() for value in
                       (gold if isinstance(gold, list) else [gold])}
        if isinstance(predicted, dict) and predicted.get("kind") == "choice":
            value = predicted.get("value")
            got = {str(item).strip().upper() for item in
                   (value if isinstance(value, list) else [value])}
            passed = got == gold_values
            return passed, f"canonical={sorted(got)} vs gold={sorted(gold_values)}"
        answer = str(pred.get("final_answer") or "")
        lead = re.search(r"^\s*(?:答案|故选|选择)?\s*[:：]?\s*([A-D])(?:\b|[.、，,])", answer, re.I)
        got = {lead.group(1).upper()} if lead else set()
        passed = got == gold_values
        return passed, f"answer={sorted(got)} vs gold={sorted(gold_values)}"
    token = str(pred.get("final_answer") or "")
    if isinstance(predicted, dict) and predicted.get("kind") == kind:
        if kind in {"scalar", "percent", "int", "bits"}:
            token = str(predicted.get("value", ""))
        elif kind == "ipv4net":
            token = f"{predicted.get('network', '')}/{predicted.get('prefix', '')}"
        elif kind == "set":
            token = ",".join(str(value) for value in predicted.get("values", []))
    result = check_numeric(token, canonical)
    return result.passed is True, result.details


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _exclude_intents(rows: list[dict], excluded: set[str]) -> list[dict]:
    return [row for row in rows if str(row.get("intent", "other")) not in excluded]


def _gold(row: dict) -> dict:
    return json.loads(row["messages"][-1]["content"])


def _user_text(row: dict) -> str:
    users = [message.get("content", "") for message in row["messages"] if message.get("role") == "user"]
    return users[-1] if users else ""


def _generation_messages(row: dict, refresh_production_prompts: bool) -> list[dict]:
    """Return frozen eval content with the current shared production prompt.

    Questions, references and gold answers stay untouched.  The system message
    is refreshed for tasks with a production contract, so evaluation matches
    the actual agent input.
    """
    messages = [dict(message) for message in row["messages"][:-1]]
    if not refresh_production_prompts:
        return messages
    intent = row.get("intent")
    if intent == "diagnose":
        content = build_diagnose_system(get_course(str(row.get("course") or "ds-algo")))
    elif intent == "grounded_qa":
        content = build_grounded_qa_system()
    else:
        return messages
    system = {"role": "system", "content": content}
    if messages and messages[0].get("role") == "system":
        messages[0] = system
    else:
        messages.insert(0, system)
    return messages


def _qa_score(pred: dict, gold: dict, user_text: str) -> tuple[bool, str]:
    expected_abstain = gold.get("abstain") is True
    citations = pred.get("citations") or []
    if expected_abstain:
        passed = pred.get("abstain") is True and not citations
        return passed, "clean_abstention" if passed else "bad_abstention"
    if pred.get("abstain") is not False or not isinstance(citations, list):
        return False, "unexpected_abstention_or_bad_citations"
    gold_citations = gold.get("citations") or []
    source_ids = {str(c.get("source_id")) for c in gold_citations if isinstance(c, dict)}
    exact = []
    compact_user = re.sub(r"\s+", "", user_text)
    for citation in citations:
        if not isinstance(citation, dict) or str(citation.get("source_id")) not in source_ids:
            continue
        evidence = str(citation.get("evidence") or "")
        if evidence and re.sub(r"\s+", "", evidence) in compact_user:
            exact.append(evidence)
    answer = str(pred.get("answer") or "")
    evidence_text = "\n".join(exact)
    supported = bool(answer) and bool(exact) and (
        len(answer) <= 20 or _evidence_overlap(answer, evidence_text) >= 0.05
    )
    return supported, f"exact={bool(exact)},overlap={round(_evidence_overlap(answer, evidence_text), 3)}"


def semantic_score(row: dict, pred: Any) -> dict:
    gold = _gold(row)
    intent = row.get("intent", "other")
    json_ok = isinstance(pred, dict)
    keys_ok = json_ok and set(gold).issubset(pred)
    task_ok, detail = False, "invalid_json"
    if json_ok and intent == "diagnose":
        gold_tags = {str(value).strip().lower() for value in (gold.get("error_tags") or [])}
        pred_tags = {str(value).strip().lower() for value in (pred.get("error_tags") or [])}
        task_ok = (pred.get("correct") is False
                   and _norm_step(pred.get("first_wrong_step")) == _norm_step(gold.get("first_wrong_step"))
                   and bool(gold_tags) and bool(gold_tags & pred_tags))
        detail = f"step={_norm_step(pred.get('first_wrong_step'))},tags={sorted(pred_tags)}"
    elif json_ok and intent == "grounded_qa":
        task_ok, detail = _qa_score(pred, gold, _user_text(row))
    elif json_ok and intent == "solve":
        canonical = gold.get("final_answer_canonical")
        if isinstance(canonical, dict):
            task_ok, detail = _solution_score(pred, canonical, _user_text(row))
        else:
            detail = "gold_missing_canonical"
    return {
        "intent": intent, "course": row.get("course", "?"),
        "json_ok": json_ok, "keys_ok": keys_ok, "task_ok": bool(task_ok), "detail": detail,
    }


def run(adapter: str, data_path: Path, tag: str, max_seq: int, max_new: int,
        refresh_production_prompts: bool = False,
        excluded_intents: set[str] | None = None) -> dict:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    pred_path = EVAL_DIR / f"targeted_semantic_predictions_{tag}.jsonl"
    report_path = EVAL_DIR / f"report_targeted_semantic_{tag}.json"
    excluded_intents = set(excluded_intents or ())
    rows = _exclude_intents(_read_jsonl(data_path), excluded_intents)
    run_signature = _semantic_signature(
        adapter, data_path, max_seq=max_seq, max_new=max_new,
        refresh_production_prompts=refresh_production_prompts,
        excluded_intents=excluded_intents,
    )
    existing = {
        row["candidate_id"]: row for row in _read_jsonl(pred_path)
        if row.get("run_signature") == run_signature and row.get("candidate_id")
    }
    model, tokenizer = _load_standalone(adapter, max_seq)
    started = time.time()
    with pred_path.open("a", encoding="utf-8") as handle:
        for index, row in enumerate(rows, 1):
            candidate_id = row["candidate_id"]
            if candidate_id not in existing:
                raw = _generate(
                    model, tokenizer,
                    _generation_messages(row, refresh_production_prompts),
                    _effective_max_new_tokens(
                        str(row.get("intent") or "other"), max_new,
                        refresh_production_prompts,
                    ),
                )
                pred = _extract_json(raw)
                result = {"candidate_id": candidate_id, "run_signature": run_signature,
                          "raw_output": raw,
                          "score": semantic_score(row, pred)}
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
                existing[candidate_id] = result
            if index % 20 == 0:
                print(f"[semantic-{tag}] {index}/{len(rows)}", flush=True)

    selected = [existing[row["candidate_id"]] for row in rows]
    groups = defaultdict(list)
    for result in selected:
        score = result["score"]
        groups[f"{score['course']}/{score['intent']}"] .append(score)

    def metrics(items: list[dict]) -> dict:
        n = len(items)
        pct = lambda key: round(100 * sum(bool(item[key]) for item in items) / n, 2) if n else 0.0
        return {"n": n, "json_valid_rate": pct("json_ok"), "keys_complete_rate": pct("keys_ok"),
                "task_accuracy": pct("task_ok")}

    all_scores = [result["score"] for result in selected]
    report = {
        "tag": tag, "adapter": adapter, "data": str(data_path), "n": len(selected),
        "excluded_intents": sorted(excluded_intents),
        "production_prompts_refreshed": refresh_production_prompts,
        "cache_version": CACHE_VERSION,
        "production_prompt_contract_fingerprint": (
            _production_prompt_contract_fingerprint()
            if refresh_production_prompts else None
        ),
        "production_task_max_tokens": (
            _production_task_limits() if refresh_production_prompts else None
        ),
        "run_signature": run_signature,
        "seconds_this_run": int(time.time() - started),
        "overall": metrics(all_scores),
        "by_group": {key: metrics(value) for key, value in sorted(groups.items())},
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="CourseLoop targeted 语义评测")
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--max-seq", type=int, default=4096)
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument(
        "--refresh-production-prompts", action="store_true",
        help="保持外部题/金标冻结，仅用当前生产 system prompt 替换诊断/QA 提示词",
    )
    parser.add_argument(
        "--exclude-intent", action="append", default=[],
        help="跳过由另一条已评测生产路由处理的 intent；可重复指定",
    )
    args = parser.parse_args()
    run(args.adapter, args.data, args.tag, args.max_seq, args.max_new_tokens,
        args.refresh_production_prompts, set(args.exclude_intent))


if __name__ == "__main__":
    main()
