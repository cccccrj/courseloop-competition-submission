"""准备 grounded QA 临时探针数据。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scripts.prepare_diagnose_probe import prepare  # noqa: E402


if __name__ == "__main__":
    print(json.dumps(prepare(
        intent="grounded_qa", candidate_prefix="grounded_qa",
        out_dir=HERE / "data" / "qa_probe",
    ), ensure_ascii=False, indent=2))
