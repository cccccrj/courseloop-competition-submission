"""给临时 grounded QA 探针划 90% 语义正确率门槛。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
SEMANTIC = HERE / "data" / "eval" / "report_targeted_semantic_qa_probe.json"
OUT = HERE / "data" / "qa_probe" / "gate.json"


def main() -> None:
    report = json.loads(SEMANTIC.read_text(encoding="utf-8"))
    accuracy = float(report["overall"]["task_accuracy"])
    repeats = 0 if accuracy >= 90.0 else (2 if accuracy < 60.0 else 1)
    result = {
        "created_at": time.strftime("%F %T"), "task_accuracy": accuracy,
        "threshold": 90.0, "passed": accuracy >= 90.0,
        "action": ("keep balanced final SFT" if accuracy >= 90.0
                   else f"oversample grounded_qa {repeats}x in final SFT"),
        "oversample_repeats": repeats,
        "adapter": "model/loras/courseloop-408-qa-probe",
        "deployment_candidate": True, "deployment_passed": accuracy >= 90.0,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from scripts.build_runtime_routes import build as build_runtime_routes
    build_runtime_routes(HERE)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
