"""比较诊断基础探针与聚焦抢救探针，并写回最终动态权重门禁。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
GATE = HERE / "data" / "diagnose_probe" / "gate.json"
RESCUE = HERE / "data" / "eval" / "report_targeted_semantic_diagnose_probe_rescue.json"
SFT_PYTHON = "/opt/sft_env/bin/python"


def _weighted_diagnose(report: dict, key: str) -> float:
    groups = [value for name, value in report.get("by_intent", {}).items()
              if name.endswith("/diagnose") and key in value]
    total = sum(int(value.get("n", 0)) for value in groups)
    return (sum(float(value[key]) * int(value.get("n", 0)) for value in groups) / total
            if total else 0.0)


def main() -> None:
    base = json.loads(GATE.read_text(encoding="utf-8"))
    rescue = json.loads(RESCUE.read_text(encoding="utf-8"))
    base_acc = float(base["task_accuracy"])
    rescue_acc = float(rescue["overall"]["task_accuracy"])
    if rescue_acc >= base_acc:
        best_acc, adapter, attempt = rescue_acc, "model/loras/courseloop-408-diagnose-probe-rescue", "rescue"
    else:
        best_acc, adapter, attempt = base_acc, base["adapter"], "base_probe"
    threshold = 90.0
    repeats = 0 if best_acc >= threshold else (2 if best_acc < 60.0 else 1)
    deployment_checks = {"target_accuracy_90": best_acc >= threshold}
    if best_acc >= threshold:
        tag = f"diagnose-probe-{attempt}-old"
        report_path = HERE / "data/eval" / f"report_lora_{tag}.json"
        if not report_path.exists():
            subprocess.run([
                SFT_PYTHON, "scripts/test_lora.py", "--adapter", adapter,
                "--val", "data/diagnose_probe/old_diag_eval.jsonl",
                "--tag", tag, "--max-new-tokens", "768",
            ], cwd=HERE, check=True)
        candidate_old = json.loads(report_path.read_text(encoding="utf-8"))
        baseline_old = json.loads((HERE / "data/eval/report_lora_best.json").read_text(encoding="utf-8"))
        deployment_checks.update({
            "old_json_drop_le_1pp": candidate_old["overall"]["json_valid_rate"]
            >= _weighted_diagnose(baseline_old, "json_valid_rate") - 1.0,
            "old_keys_drop_le_1pp": candidate_old["overall"]["keys_complete_rate"]
            >= _weighted_diagnose(baseline_old, "keys_complete_rate") - 1.0,
            "old_step_drop_le_2pp": _weighted_diagnose(candidate_old, "first_wrong_step_acc")
            >= _weighted_diagnose(baseline_old, "first_wrong_step_acc") - 2.0,
            "old_tag_drop_le_2pp": _weighted_diagnose(candidate_old, "error_tag_consistency")
            >= _weighted_diagnose(baseline_old, "error_tag_consistency") - 2.0,
        })
    deployment_passed = all(deployment_checks.values())
    result = {
        "created_at": time.strftime("%F %T"), "task_accuracy": best_acc,
        "threshold": threshold, "passed": best_acc >= threshold,
        "best_attempt": attempt, "adapter": adapter, "deployment_candidate": True,
        "deployment_passed": deployment_passed, "deployment_checks": deployment_checks,
        "oversample_course": "ds-algo", "oversample_repeats": repeats,
        "action": ("keep balanced final SFT" if best_acc >= threshold
                   else f"oversample ds-algo diagnose {repeats}x in final SFT"),
        "attempts": {"base_probe": base_acc, "rescue": rescue_acc},
    }
    GATE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from scripts.build_runtime_routes import build as build_runtime_routes
    build_runtime_routes(HERE)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
