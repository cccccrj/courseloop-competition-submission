"""QA 基础探针失败后自动排队一次低学习率聚焦抢救。"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
GATE = HERE / "data" / "qa_probe" / "gate.json"
MARKER = HERE / "data" / "qa_probe_rescue" / "queued.json"
RAG_PYTHON = "/opt/projects/conda_data/envs/rag_env/bin/python"
SFT_PYTHON = "/opt/sft_env/bin/python"
QUEUE = "/opt/gpu_queue/submit.sh"


def main(timeout_hours: float = 3.0) -> None:
    if MARKER.exists():
        print("QA rescue 已处理", flush=True)
        return
    deadline = time.time() + timeout_hours * 3600
    while not GATE.exists():
        if time.time() >= deadline:
            raise TimeoutError("等待 QA 探针 gate 超时")
        time.sleep(15)
    gate = json.loads(GATE.read_text(encoding="utf-8"))
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    if gate.get("passed"):
        MARKER.write_text(json.dumps({"skipped": True, "reason": "base probe passed"}, indent=2) + "\n")
        return
    subprocess.run([RAG_PYTHON, "scripts/prepare_qa_rescue_probe.py"], cwd=HERE, check=True)
    command = (
        f"{SFT_PYTHON} scripts/train_sft.py "
        "--adapter model/loras/courseloop-408-qa-probe "
        "--train data/qa_probe_rescue/train.jsonl --val /tmp/courseloop-no-val.jsonl "
        "--out model/loras/courseloop-408-qa-probe-rescue "
        "--checkpoint-dir model/_sft_qa_probe_rescue_ckpt "
        "--epochs 1 --lr 2e-5 --eval-strategy epoch --no-test"
        f" && {SFT_PYTHON} scripts/eval_targeted_semantic.py "
        "--adapter model/loras/courseloop-408-qa-probe-rescue "
        "--data data/qa_probe/targeted_eval.jsonl "
        "--tag qa_probe_rescue --max-new-tokens 768"
        f" && {RAG_PYTHON} scripts/evaluate_qa_probe_rescue.py"
    )
    result = subprocess.run(
        ["bash", QUEUE, "courseloop-qa-probe-rescue", str(HERE), command, "1"],
        cwd=HERE, check=True, capture_output=True, text=True)
    MARKER.write_text(json.dumps({"created_at": time.strftime("%F %T"),
                                  "submit_output": result.stdout},
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(result.stdout, flush=True)


if __name__ == "__main__":
    main()
