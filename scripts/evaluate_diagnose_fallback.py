"""Gate the taxonomy diagnosis LoRA as a fallback, not as a universal override."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

EVAL = HERE / "data/eval"
GATE = HERE / "data/diagnose_probe/gate.json"
ADAPTER = "model/loras/courseloop-408-diagnose-taxonomy-rescue"


def _weighted_diagnose(report: dict) -> dict:
    groups = [metrics for group, metrics in (report.get("by_intent") or {}).items()
              if group.rsplit("/", 1)[-1] == "diagnose"]
    n = sum(int(metrics.get("n", 0)) for metrics in groups)
    keys = ("json_valid_rate", "keys_complete_rate", "first_wrong_step_acc",
            "error_tag_consistency")
    return {"n": n, **{
        key: round(sum(int(metrics.get("n", 0)) * float(metrics.get(key, 0.0))
                       for metrics in groups) / max(1, n), 2)
        for key in keys
    }}


def fallback_decision(targeted: dict, baseline_targeted: dict,
                      old_regression: dict, candidate_regression: dict) -> dict:
    target = targeted["overall"]
    base_target = baseline_targeted["by_group"]["ds-algo/diagnose"]
    old = _weighted_diagnose(old_regression)
    candidate = _weighted_diagnose(candidate_regression)
    checks = {
        "targeted_accuracy_70": float(target.get("task_accuracy", 0)) >= 70.0,
        "targeted_gain_25pp": (
            float(target.get("task_accuracy", 0))
            >= float(base_target.get("task_accuracy", 0)) + 25.0
        ),
        "targeted_structure_99": (
            float(target.get("json_valid_rate", 0)) >= 99.0
            and float(target.get("keys_complete_rate", 0)) >= 99.0
        ),
        "same_regression_population": old["n"] == candidate["n"] == 153,
        "production_prompts_refreshed": (
            old_regression.get("production_prompts_refreshed") is True
            and candidate_regression.get("production_prompts_refreshed") is True
        ),
        "old_json_drop_le_1pp": candidate["json_valid_rate"] >= old["json_valid_rate"] - 1.0,
        "old_keys_drop_le_1pp": candidate["keys_complete_rate"] >= old["keys_complete_rate"] - 1.0,
        "old_step_drop_le_2pp": (
            candidate["first_wrong_step_acc"] >= old["first_wrong_step_acc"] - 2.0
        ),
        "old_tag_drop_le_2pp": (
            candidate["error_tag_consistency"] >= old["error_tag_consistency"] - 2.0
        ),
    }
    return {
        "created_at": time.strftime("%F %T"),
        "checks": checks,
        "passed": all(checks.values()),
        "targeted": target,
        "baseline_targeted": base_target,
        "old_regression": old,
        "candidate_regression": candidate,
        "policy": "use only when final selected default remains courseloop-408",
    }


def main() -> None:
    targeted = json.loads((EVAL / "report_targeted_semantic_diagnose_taxonomy_rescue.json").read_text())
    baseline = json.loads((EVAL / "report_targeted_semantic_targeted_baseline-prodprompt.json").read_text())
    old = json.loads((EVAL / "report_lora_diagnose-old-prodprompt-regression.json").read_text())
    candidate = json.loads(
        (EVAL / "report_lora_diagnose-taxonomy-prodprompt-regression.json").read_text())
    result = fallback_decision(targeted, baseline, old, candidate)
    gate = json.loads(GATE.read_text()) if GATE.exists() else {}
    gate.update({
        "fallback_evaluated_at": result["created_at"],
        "fallback_passed": result["passed"],
        "fallback_adapter": ADAPTER,
        "fallback_report": "data/eval/diagnose_fallback_selection.json",
    })
    GATE.write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n")
    (EVAL / "diagnose_fallback_selection.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    from scripts.build_runtime_routes import build
    build(HERE)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
