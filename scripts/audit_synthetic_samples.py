"""对已过确定性门禁的合成样本做 50 条独立教师盲审。

Kimi 生成的 diagnose/QA 交给 Qwen 审；Qwen 生成的 practice 交给 Kimi 审；
solve 已由两模型独立求解，仅抽查结构与解释。按条落盘，可断点续跑。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scripts.generate_sft_pilot import KIMI, QWEN, _append, _jsonl, _sha  # noqa: E402
from scripts.run_targeted_synthetic_pilot import _validated_call  # noqa: E402


DEFAULT_INPUT = HERE / "data" / "synthetic_targeted_200" / "accepted.jsonl"
DEFAULT_OUT = HERE / "data" / "synthetic_targeted_200" / "blind_audit.jsonl"
DEFAULT_REPORT = HERE / "data" / "synthetic_targeted_200" / "blind_audit_report.json"
REQUIRED = {"accept", "severe_error", "issues", "reason"}


def _select(rows: list[dict], n: int, excluded_ids: set[str] | None = None) -> list[dict]:
    """先按 intent×course 各取一条，再用稳定哈希补齐。"""
    excluded_ids = set(excluded_ids or ())
    rows = [row for row in rows if str(row.get("candidate_id")) not in excluded_ids]
    ordered = sorted(rows, key=lambda row: _sha(["audit", row.get("candidate_id")]))
    selected = []
    seen_groups = set()
    for row in ordered:
        group = (row.get("intent"), row.get("course"))
        if group not in seen_groups:
            selected.append(row)
            seen_groups.add(group)
    used = {row.get("candidate_id") for row in selected}
    selected.extend(row for row in ordered if row.get("candidate_id") not in used)
    return selected[:n]


def _judge_model(row: dict) -> str:
    # practice 的教师是 Qwen，其余单教师任务是 Kimi；用相反模型审查。
    return KIMI if row.get("intent") == "practice" else QWEN


def _audit(row: dict) -> dict:
    model = _judge_model(row)
    transcript = json.dumps(row.get("messages") or [], ensure_ascii=False)
    messages = [
        {"role": "system", "content": (
            "你是独立教学数据审计员。严格判断该训练样本是否可用于 408 课程模型训练。"
            "重点检查：题目/回答是否自洽、最终答案是否正确、代码是否明显错误、诊断是否定位第一个错误、"
            "RAG 引用是否为材料原文且回答受证据支持、JSON 是否符合系统要求。"
            "severe_error 仅在错误答案、伪造证据、参考代码错误、诊断方向颠倒时为 true。"
            "只输出完整 JSON：{\"accept\":true,\"severe_error\":false,"
            "\"issues\":[],\"reason\":\"简短依据\"}。"
        )},
        {"role": "user", "content": f"请盲审以下一条训练对话：\n{transcript}"},
    ]
    calls, parsed, schema_ok = _validated_call(model, messages, REQUIRED, 0.0, 1600)
    accepted = (schema_ok and parsed.get("accept") is True and parsed.get("severe_error") is False
                and not any(call["error"] for call in calls))
    return {
        "candidate_id": row.get("candidate_id"), "course": row.get("course"),
        "intent": row.get("intent"), "judge_model": model, "calls": calls,
        "parsed": parsed, "schema_ok": schema_ok, "accepted": accepted,
        "severe_error": bool(parsed.get("severe_error")) if isinstance(parsed, dict) else True,
        "created_at": time.strftime("%F %T"),
    }


def run(input_path: Path, out_path: Path, report_path: Path, n: int, workers: int) -> dict:
    import concurrent.futures

    rows = _jsonl(input_path)
    excluded_path = out_path.parent / "blind_audit_excluded_ids.json"
    excluded_payload = (json.loads(excluded_path.read_text(encoding="utf-8"))
                        if excluded_path.exists() else {})
    excluded_ids = set(excluded_payload.get("candidate_ids", []))
    selected = _select(rows, n, excluded_ids)
    if len(selected) < n:
        raise RuntimeError(f"可审样本仅 {len(selected)} < {n}")
    existing = _jsonl(out_path)
    done = {row["candidate_id"] for row in existing}
    pending = [row for row in selected if row["candidate_id"] not in done]
    print(f"[blind-audit] 已有 {len(existing)}，待审 {len(pending)} / {n}", flush=True)
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_audit, row): row for row in pending}
            for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                result = future.result()
                _append(out_path, result)
                print(f"\r[blind-audit] {i}/{len(pending)} accepted={result['accepted']}   ",
                      end="", flush=True)
        print()
    audited_by_id = {row["candidate_id"]: row for row in _jsonl(out_path)}
    audited = [audited_by_id[row["candidate_id"]] for row in selected if row["candidate_id"] in audited_by_id]
    calls = [call for row in audited for call in row.get("calls", [])]
    accepted = sum(row.get("accepted") is True for row in audited)
    severe = sum(row.get("severe_error") is True for row in audited)
    schema = sum(row.get("schema_ok") is True for row in audited)
    api_ok = sum(not call.get("error") for call in calls)
    by_intent = {}
    for intent in sorted({row.get("intent") for row in audited}):
        subset = [row for row in audited if row.get("intent") == intent]
        by_intent[intent] = {"n": len(subset), "accepted": sum(row["accepted"] for row in subset)}
    history_path = out_path.parent / "blind_audit_history.json"
    history = (json.loads(history_path.read_text(encoding="utf-8"))
               if history_path.exists() else {"rounds": []})
    prior_rounds = history.get("rounds", [])
    prior_n = sum(int(item.get("n", 0)) for item in prior_rounds)
    prior_accepted = sum(int(item.get("accepted", 0)) for item in prior_rounds)
    cumulative_n = prior_n + len(audited)
    cumulative_accepted = prior_accepted + accepted
    report = {
        "created_at": time.strftime("%F %T"), "n": len(audited),
        "accepted": accepted,
        "accept_rate": round(100 * accepted / len(audited), 2) if audited else 0,
        "severe_errors": severe,
        "schema_rate": round(100 * schema / len(audited), 2) if audited else 0,
        "api_success_rate": round(100 * api_ok / len(calls), 2) if calls else 0,
        "excluded_prior_audits": len(excluded_ids),
        "cumulative_n": cumulative_n,
        "cumulative_accepted": cumulative_accepted,
        "cumulative_accept_rate": round(100 * cumulative_accepted / cumulative_n, 2)
        if cumulative_n else 0,
        "by_intent": by_intent,
        "issue_counts": dict(Counter(issue for row in audited
                                     for issue in ((row.get("parsed") or {}).get("issues") or []))),
    }
    report["passed"] = (
        report["n"] == n and report["accept_rate"] >= 90.0 and severe == 0
        and report["cumulative_accept_rate"] >= 90.0
        and report["schema_rate"] >= 98.0 and report["api_success_rate"] >= 99.0
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="CourseLoop 合成数据独立教师盲审")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    run(args.input, args.out, args.report, args.n, max(1, args.workers))


if __name__ == "__main__":
    main()
