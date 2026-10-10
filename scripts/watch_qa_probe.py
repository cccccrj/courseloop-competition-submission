"""等待四门 grounded QA 配额完成后，准备并排队一次 GPU 能力探针。"""
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
MARKER = HERE / "data" / "qa_probe" / "queued.json"


def _accepted(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        bool(row.get("accepted") and row.get("training_sample"))
        for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    )


def main(timeout_hours: float = 6.0) -> None:
    if MARKER.exists():
        print("QA probe 已排队", flush=True)
        return
    deadline = time.time() + timeout_hours * 3600
    while time.time() < deadline:
        counts = {
            course: _accepted(HERE / "data" / "synthetic_bulk_3000" / "candidates"
                              / f"grounded_qa_{course}.jsonl")
            for course in COURSES
        }
        print(f"[qa-probe-watch] {counts}", flush=True)
        if all(value >= 225 for value in counts.values()):
            subprocess.run([RAG_PYTHON, "scripts/prepare_qa_probe.py"], cwd=HERE, check=True)
            command = (
                f"{SFT_PYTHON} scripts/train_sft.py "
                "--adapter model/loras/courseloop-408 "
                "--train data/qa_probe/train.jsonl --val /tmp/courseloop-no-val.jsonl "
                "--out model/loras/courseloop-408-qa-probe "
                "--checkpoint-dir model/_sft_qa_probe_ckpt "
                "--epochs 1 --lr 5e-5 --eval-strategy epoch --no-test"
                f" && {SFT_PYTHON} scripts/eval_targeted_semantic.py "
                "--adapter model/loras/courseloop-408-qa-probe "
                "--data data/qa_probe/targeted_eval.jsonl "
                "--tag qa_probe --max-new-tokens 768"
                f" && {RAG_PYTHON} scripts/evaluate_qa_probe.py"
            )
            result = subprocess.run(
                ["bash", QUEUE, "courseloop-qa-probe", str(HERE), command, "1"],
                cwd=HERE, check=True, capture_output=True, text=True)
            MARKER.parent.mkdir(parents=True, exist_ok=True)
            MARKER.write_text(json.dumps({"created_at": time.strftime("%F %T"),
                                          "counts": counts, "submit_output": result.stdout},
                                         ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(result.stdout, flush=True)
            return
        time.sleep(30)
    raise TimeoutError("等待 grounded QA 配额超过 6 小时")


if __name__ == "__main__":
    main()
