"""等待四门 bulk diagnose 配额完成后，准备并排队一次 GPU 学习率探针。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scripts.generate_sft_pilot import COURSES  # noqa: E402


QUEUE = "/opt/gpu_queue/submit.sh"
RAG_PYTHON = "/opt/projects/conda_data/envs/rag_env/bin/python"
SFT_PYTHON = "/opt/sft_env/bin/python"
MARKER = HERE / "data" / "diagnose_probe" / "queued.json"


def _accepted(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        bool(row.get("accepted") and row.get("training_sample"))
        for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    )


def main(timeout_hours: float = 3.0) -> None:
    if MARKER.exists():
        print("diagnose probe 已排队", flush=True)
        return
    deadline = time.time() + timeout_hours * 3600
    while time.time() < deadline:
        counts = {
            course: _accepted(HERE / "data" / "synthetic_bulk_3000" / "candidates"
                              / f"diagnose_{course}.jsonl")
            for course in COURSES
        }
        print(f"[diagnose-probe-watch] {counts}", flush=True)
        if all(value >= 225 for value in counts.values()):
            subprocess.run([RAG_PYTHON, "scripts/prepare_diagnose_probe.py"], cwd=HERE, check=True)
            command = (
                f"{SFT_PYTHON} scripts/train_sft.py "
                "--adapter model/loras/courseloop-408 "
                "--train data/diagnose_probe/train.jsonl --val data/sft/val.jsonl "
                "--out model/loras/courseloop-408-diagnose-probe "
                "--checkpoint-dir model/_sft_diagnose_probe_ckpt "
                "--epochs 1 --lr 5e-5 --eval-strategy epoch --no-test"
                f" && {SFT_PYTHON} scripts/eval_targeted_semantic.py "
                "--adapter model/loras/courseloop-408-diagnose-probe "
                "--data data/diagnose_probe/targeted_diagnose.jsonl "
                "--tag diagnose_probe --max-new-tokens 768"
                f" && {RAG_PYTHON} scripts/evaluate_diagnose_probe.py"
            )
            result = subprocess.run(
                ["bash", QUEUE, "courseloop-diagnose-probe", str(HERE), command, "1"],
                cwd=HERE, check=True, capture_output=True, text=True)
            MARKER.parent.mkdir(parents=True, exist_ok=True)
            MARKER.write_text(json.dumps({"created_at": time.strftime("%F %T"),
                                          "counts": counts, "submit_output": result.stdout},
                                         ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(result.stdout, flush=True)
            return
        time.sleep(30)
    raise TimeoutError("等待 diagnose 配额超过 3 小时")


if __name__ == "__main__":
    main()
