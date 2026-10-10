"""独立 GPU 演示部署；默认只监听回环，不修改生产或现有训练环境。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def model_command(executable, port=18003):
    base, adapter = ROOT / "model/Qwen3-14B-FP8", ROOT / "model/courseloop-408"
    assert (base / "model.safetensors.index.json").is_file()
    assert (adapter / "adapter_model.safetensors").is_file()
    return [executable, "serve", str(base), "--host", "127.0.0.1", "--port", str(port),
            "--quantization", "fp8", "--dtype", "bfloat16", "--enable-lora",
            "--lora-modules", f"courseloop-default={adapter}", "--max-lora-rank", "16",
            "--max-loras", "1", "--max-model-len", "4096", "--max-num-seqs", "8",
            "--max-num-batched-tokens", "4096", "--gpu-memory-utilization", "0.90",
            "--enforce-eager", "--no-enable-prefix-caching", "--disable-log-requests",
            "--served-model-name", "qwen3-14b-fp8-base", "--generation-config", "vllm"]


def configure(commit, port):
    assert not (ROOT / ".git").exists(), "配置仅用于独立导出的部署目录"
    assert "courseloop-gpu-submission-" in ROOT.name
    from dotenv import set_key
    from scripts.submission_prepare import bootstrap
    if not (ROOT / "data/processed/courseloop.db").exists():
        bootstrap()
    env = ROOT / ".env"
    values = dict(CL_BUILD_COMMIT=commit, CL_RAG_MODE="fts5",
                  CL_LLM_TEXT_BASE_URL=f"http://127.0.0.1:{port}/v1",
                  CL_LLM_TEXT_API_KEY="local-demo",
                  CL_LLM_BASE_URL=f"http://127.0.0.1:{port}/v1",
                  CL_LLM_API_KEY="local-demo", CL_LLM_NO_THINK="true",
                  CL_LLM_WORKHORSE_MODEL="courseloop-default",
                  CL_LLM_EXPERT_MODEL="courseloop-default",
                  CL_LLM_REASONER_MODEL="courseloop-default")
    for task in ("SOLVE", "PRACTICE", "DIAGNOSE", "GROUNDED_QA"):
        values[f"CL_LLM_{task}_MODEL"] = "courseloop-default"
    for key, value in values.items():
        set_key(str(env), key, value)
    env.chmod(0o600)
    print("独立演示环境已配置本地GPU文本模型；凭据与原始环境文件保持私有。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["configure", "model", "app"])
    parser.add_argument("--source-commit")
    parser.add_argument("--model-port", type=int, default=18003)
    parser.add_argument("--app-port", type=int, default=18080)
    parser.add_argument("--vllm", default="vllm")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.mode == "configure":
        assert args.source_commit and len(args.source_commit) == 40
        configure(args.source_commit, args.model_port)
    elif args.mode == "model":
        cmd = model_command(args.vllm, args.model_port)
        (ROOT / ".private").mkdir(exist_ok=True)
        (ROOT / ".private/gpu-service-command.json").write_text(json.dumps(cmd, indent=2)+"\n")
        env = dict(os.environ, CUDA_VISIBLE_DEVICES="0", OMP_NUM_THREADS="8",
                   VLLM_WORKER_MULTIPROC_METHOD="spawn", VLLM_USE_V1="1",
                   VLLM_NO_USAGE_STATS="1", DO_NOT_TRACK="1")
        os.execvpe(cmd[0], cmd, env)
    else:
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "scripts.submission_serve:app",
                                "--host", "127.0.0.1", "--port", str(args.app_port), "--workers", "1"])


if __name__ == "__main__":
    main()
