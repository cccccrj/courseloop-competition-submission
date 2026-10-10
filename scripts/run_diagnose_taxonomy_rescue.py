"""Conditionally run the final diagnosis taxonomy rescue inside the GPU queue."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
GATE = HERE / "data/diagnose_probe/gate.json"
RAG_PYTHON = "/opt/projects/conda_data/envs/rag_env/bin/python"
SFT_PYTHON = "/opt/sft_env/bin/python"
OUT = "model/loras/courseloop-408-diagnose-taxonomy-rescue"


def _run(command: list[str]) -> None:
    print("[diagnose-taxonomy-rescue] RUN:", " ".join(command), flush=True)
    subprocess.run(command, cwd=HERE, check=True)


def main() -> None:
    gate = json.loads(GATE.read_text(encoding="utf-8"))
    if gate.get("deployment_passed"):
        print("[diagnose-taxonomy-rescue] Existing diagnosis specialist passed; skip.")
        return
    _run([RAG_PYTHON, "scripts/prepare_diagnose_taxonomy_rescue.py"])
    adapter = str(gate.get("adapter") or "model/loras/courseloop-408-diagnose-probe-rescue")
    out_path = HERE / OUT
    if not ((out_path / "adapter_config.json").exists()
            and (out_path / "adapter_model.safetensors").exists()):
        _run([
            SFT_PYTHON, "scripts/train_sft.py", "--adapter", adapter,
            "--train", "data/diagnose_taxonomy_rescue/train.jsonl",
            "--val", "/tmp/courseloop-no-val.jsonl", "--out", OUT,
            "--checkpoint-dir", "model/_sft_diagnose_taxonomy_rescue_ckpt",
            "--epochs", "1", "--lr", "1.5e-5", "--eval-strategy", "epoch", "--no-test",
        ])
    _run([
        SFT_PYTHON, "scripts/eval_targeted_semantic.py", "--adapter", OUT,
        "--data", "data/diagnose_probe/targeted_diagnose.jsonl",
        "--tag", "diagnose_taxonomy_rescue", "--max-new-tokens", "768",
        "--refresh-production-prompts",
    ])
    _run([RAG_PYTHON, "scripts/evaluate_diagnose_taxonomy_rescue.py"])


if __name__ == "__main__":
    main()
