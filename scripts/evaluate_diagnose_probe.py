"""给临时诊断探针划门槛，供正式增量数据动态加权。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
SEMANTIC = HERE / "data" / "eval" / "report_targeted_semantic_diagnose_probe.json"
OUT = HERE / "data" / "diagnose_probe" / "gate.json"


def main() -> None:
    report = json.loads(SEMANTIC.read_text(encoding="utf-8"))
    accuracy = float(report["overall"]["task_accuracy"])
    repeats = 0 if accuracy >= 90.0 else (2 if accuracy < 60.0 else 1)
    result = {
        "created_at": time.strftime("%F %T"), "task_accuracy": accuracy,
        "threshold": 90.0, "passed": accuracy >= 90.0,
        "action": ("keep balanced final SFT" if accuracy >= 90.0
                   else f"oversample ds-algo diagnose {repeats}x in final SFT"),
        "oversample_course": "ds-algo",
        "oversample_repeats": repeats,
        "adapter": "model/loras/courseloop-408-diagnose-probe",
        "deployment_candidate": False, "deployment_passed": False,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from scripts.build_runtime_routes import build as build_runtime_routes
    build_runtime_routes(HERE)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
