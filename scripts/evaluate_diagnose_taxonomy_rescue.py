"""Gate the taxonomy-contract diagnosis rescue and refresh runtime routes."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scripts.evaluate_diagnose_probe_rescue import _weighted_diagnose  # noqa: E402


GATE = HERE / "data/diagnose_probe/gate.json"
REPORT = HERE / "data/eval/report_targeted_semantic_diagnose_taxonomy_rescue.json"
ADAPTER = "model/loras/courseloop-408-diagnose-taxonomy-rescue"
SFT_PYTHON = "/opt/sft_env/bin/python"


def main() -> None:
    previous = json.loads(GATE.read_text(encoding="utf-8"))
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    previous_acc = float(previous["task_accuracy"])
    taxonomy_acc = float(report["overall"]["task_accuracy"])
    if taxonomy_acc >= previous_acc:
        best_acc, adapter, attempt = taxonomy_acc, ADAPTER, "taxonomy_rescue"
    else:
        best_acc, adapter = previous_acc, str(previous["adapter"])
        attempt = str(previous.get("best_attempt") or "previous")

    threshold = 90.0
    checks = {"target_accuracy_90": best_acc >= threshold}
    if best_acc >= threshold:
        tag = f"diagnose-{attempt}-old"
        old_report_path = HERE / f"data/eval/report_lora_{tag}.json"
        if not old_report_path.exists():
            subprocess.run([
                SFT_PYTHON, "scripts/test_lora.py", "--adapter", adapter,
                "--val", "data/diagnose_probe/old_diag_eval.jsonl",
                "--tag", tag, "--max-new-tokens", "768",
            ], cwd=HERE, check=True)
        candidate_old = json.loads(old_report_path.read_text(encoding="utf-8"))
        baseline_old = json.loads(
            (HERE / "data/eval/report_lora_best.json").read_text(encoding="utf-8"))
        checks.update({
            "old_json_drop_le_1pp": candidate_old["overall"]["json_valid_rate"]
            >= _weighted_diagnose(baseline_old, "json_valid_rate") - 1.0,
            "old_keys_drop_le_1pp": candidate_old["overall"]["keys_complete_rate"]
            >= _weighted_diagnose(baseline_old, "keys_complete_rate") - 1.0,
            "old_step_drop_le_2pp": _weighted_diagnose(candidate_old, "first_wrong_step_acc")
            >= _weighted_diagnose(baseline_old, "first_wrong_step_acc") - 2.0,
            "old_tag_drop_le_2pp": _weighted_diagnose(candidate_old, "error_tag_consistency")
            >= _weighted_diagnose(baseline_old, "error_tag_consistency") - 2.0,
        })

    attempts = dict(previous.get("attempts") or {})
    attempts["taxonomy_rescue"] = taxonomy_acc
    passed = best_acc >= threshold
    result = {
        "created_at": time.strftime("%F %T"),
        "task_accuracy": best_acc,
        "threshold": threshold,
        "passed": passed,
        "best_attempt": attempt,
        "adapter": adapter,
        "deployment_candidate": True,
        "deployment_passed": all(checks.values()),
        "deployment_checks": checks,
        "oversample_course": "ds-algo",
        "oversample_repeats": 0 if passed else (2 if best_acc < 60.0 else 1),
        "action": ("keep balanced final SFT" if passed else
                   f"oversample ds-algo diagnose {2 if best_acc < 60.0 else 1}x in final SFT"),
        "attempts": attempts,
    }
    GATE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from scripts.build_runtime_routes import build as build_runtime_routes
    build_runtime_routes(HERE)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
