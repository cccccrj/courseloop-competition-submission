"""Exhaustively verify every selected bulk training sample with an independent teacher.

Results are append-only and resumable. Rejected candidate IDs are merged into the
same quarantine denylist consumed by ``generate_synthetic_bulk.py``; after a
re-selection, rerunning this script verifies only newly introduced replacements.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(HERE))

from scripts.audit_synthetic_samples import _audit  # noqa: E402
from scripts.generate_sft_pilot import PRICES, _append, _jsonl  # noqa: E402


BULK = HERE / "data" / "synthetic_bulk_3000"
DEFAULT_INPUT = BULK / "accepted.jsonl"
DEFAULT_STATE = BULK / "semantic_verification.jsonl"
DEFAULT_REPORT = BULK / "semantic_verification_report.json"
DEFAULT_REJECTED = BULK / "blind_audit_rejected_ids.json"
TARGET_INTENTS = {"practice", "diagnose", "grounded_qa", "solve"}
TARGET_COUNT = 3000


def run(input_path: Path, state_path: Path, report_path: Path,
        rejected_path: Path, workers: int) -> dict:
    selected = [row for row in _jsonl(input_path) if row.get("intent") in TARGET_INTENTS]
    if len(selected) != TARGET_COUNT:
        raise RuntimeError(f"selected bulk samples must be exactly {TARGET_COUNT}, got {len(selected)}")
    existing_rows = _jsonl(state_path)
    existing = {str(row.get("candidate_id")): row for row in existing_rows}
    pending = [row for row in selected if str(row.get("candidate_id")) not in existing]
    print(f"[semantic-verify] cached={len(selected) - len(pending)} pending={len(pending)} / {TARGET_COUNT}",
          flush=True)
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_audit, row): row for row in pending}
            for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                result = future.result()
                _append(state_path, result)
                existing[str(result.get("candidate_id"))] = result
                if i % 25 == 0 or i == len(pending):
                    print(f"[semantic-verify] {i}/{len(pending)}", flush=True)

    current = [existing.get(str(row.get("candidate_id"))) for row in selected]
    rejected_current = [row for row in current if not row or row.get("accepted") is not True]
    payload = json.loads(rejected_path.read_text(encoding="utf-8")) if rejected_path.exists() else {}
    rejected_ids = set(map(str, payload.get("candidate_ids", [])))
    rejected_ids.update(str(row.get("candidate_id")) for row in rejected_current if row)
    rejected_path.write_text(json.dumps({
        "updated_at": time.strftime("%F %T"),
        "candidate_ids": sorted(rejected_ids),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    calls = [call for row in current if row for call in row.get("calls", [])]
    tokens = {
        key: sum(int((call.get("usage") or {}).get(key, 0) or 0) for call in calls)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    cost_by_model: dict[str, float] = {}
    for call in calls:
        model = str(call.get("model") or "")
        if model not in PRICES:
            continue
        input_price, output_price = PRICES[model]
        usage = call.get("usage") or {}
        cost_by_model[model] = cost_by_model.get(model, 0.0) + (
            int(usage.get("prompt_tokens", 0) or 0) * input_price
            + int(usage.get("completion_tokens", 0) or 0) * output_price
        ) / 1_000_000
    report = {
        "created_at": time.strftime("%F %T"),
        "selected_target": len(selected),
        "cached_before_run": len(selected) - len(pending),
        "newly_verified": len(pending),
        "verified_accepted": sum(bool(row and row.get("accepted") is True) for row in current),
        "rejected_current": len(rejected_current),
        "rejected_total": len(rejected_ids),
        "api_calls_current_selection": len(calls),
        "token_usage_current_selection": tokens,
        "cost_by_model_cny": {key: round(value, 4) for key, value in cost_by_model.items()},
        "cost_cny_current_selection": round(sum(cost_by_model.values()), 4),
        "api_success_rate": round(
            100 * sum(not call.get("error") for call in calls) / len(calls), 2
        ) if calls else 0,
    }
    report["passed"] = (
        report["selected_target"] == TARGET_COUNT
        and report["verified_accepted"] == TARGET_COUNT
        and report["rejected_current"] == 0
        and report["api_success_rate"] >= 99.0
    )
    report["next"] = "blind audit" if report["passed"] else "reselect replacements and rerun"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="全量复核 bulk 全部 3,000 条训练样本")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--rejected", type=Path, default=DEFAULT_REJECTED)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    run(args.input, args.state, args.report, args.rejected, args.workers)


if __name__ == "__main__":
    main()
