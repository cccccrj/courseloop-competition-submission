"""比较 QA 基础/抢救探针，并写回正式训练使用的最终门禁。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
GATE = HERE / "data" / "qa_probe" / "gate.json"
RESCUE = HERE / "data" / "eval" / "report_targeted_semantic_qa_probe_rescue.json"
PRODUCTION = HERE / "data" / "eval" / "report_targeted_semantic_qa_probe_rescue-prodprompt.json"
BASE_REPORT = HERE / "data" / "eval" / "report_targeted_semantic_qa_probe.json"


def main() -> None:
    base = json.loads(GATE.read_text(encoding="utf-8"))
    rescue = json.loads(RESCUE.read_text(encoding="utf-8"))
    base_report = json.loads(BASE_REPORT.read_text(encoding="utf-8"))
    base_acc = float(base_report["overall"]["task_accuracy"])
    rescue_acc = float(rescue["overall"]["task_accuracy"])
    if rescue_acc >= base_acc:
        best_acc, adapter, attempt = rescue_acc, "model/loras/courseloop-408-qa-probe-rescue", "rescue"
    else:
        best_acc, adapter, attempt = base_acc, base["adapter"], "base_probe"
    threshold = 90.0
    repeats = 0 if best_acc >= threshold else (2 if best_acc < 60.0 else 1)
    best_report = rescue if attempt == "rescue" else base_report
    production_report = (
        json.loads(PRODUCTION.read_text(encoding="utf-8"))
        if attempt == "rescue" and PRODUCTION.exists() else best_report
    )
    overall = production_report.get("overall") or {}
    deployment_checks = {
        "production_prompts_refreshed": (
            production_report.get("production_prompts_refreshed") is True
        ),
        "target_accuracy_90": float(overall.get("task_accuracy", 0.0)) >= threshold,
        "json_valid_95": float(overall.get("json_valid_rate", 0.0)) >= 95.0,
        "keys_complete_95": float(overall.get("keys_complete_rate", 0.0)) >= 95.0,
    }
    deployment_passed = all(deployment_checks.values())
    result = {
        "created_at": time.strftime("%F %T"), "task_accuracy": best_acc,
        "threshold": threshold, "passed": best_acc >= threshold,
        "best_attempt": attempt, "adapter": adapter, "deployment_candidate": True,
        "deployment_passed": deployment_passed,
        "deployment_checks": deployment_checks,
        "production_prompts_refreshed": (
            production_report.get("production_prompts_refreshed") is True
        ),
        "production_report": (
            str(PRODUCTION.relative_to(HERE)) if PRODUCTION.exists() else "pending"
        ),
        "deployment_task_accuracy": float(overall.get("task_accuracy", 0.0)),
        "json_valid_rate": float(overall.get("json_valid_rate", 0.0)),
        "keys_complete_rate": float(overall.get("keys_complete_rate", 0.0)),
        "oversample_repeats": repeats,
        "action": ("keep balanced final SFT" if best_acc >= threshold
                   else f"oversample grounded_qa {repeats}x in final SFT"),
        "attempts": {"base_probe": base_acc, "rescue": rescue_acc},
    }
    GATE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from scripts.build_runtime_routes import build as build_runtime_routes
    build_runtime_routes(HERE)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
