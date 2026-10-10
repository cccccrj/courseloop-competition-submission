"""单基座、多 LoRA 的 CourseLoop vLLM 启动器；不会在训练流水线中自动占卡。"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scripts.build_runtime_routes import build  # noqa: E402


def command(port: int, gpu: str, vllm: str) -> tuple[list[str], dict]:
    routes = build(HERE)
    cmd = [
        vllm, "serve", routes["base_model"],
        "--quantization", "bitsandbytes", "--load-format", "bitsandbytes",
        "--enable-lora", "--lora-modules",
    ]
    cmd.extend(f"{alias}={path}" for alias, path in routes["adapters"].items())
    cmd.extend([
        "--max-lora-rank", "16", "--max-loras", "1",
        "--max-cpu-loras", str(max(2, len(routes["adapters"]))),
        "--max-model-len", "4096", "--gpu-memory-utilization", "0.75",
        "--served-model-name", "qwen3-14b-base", "--port", str(port),
    ])
    return cmd, routes


def main() -> None:
    parser = argparse.ArgumentParser(description="CourseLoop 多 LoRA vLLM 服务")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--gpu", default="0")
    parser.add_argument("--vllm", default="/opt/miniconda3/envs/rag_env/bin/vllm")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cmd, routes = command(args.port, args.gpu, args.vllm)
    print(json.dumps(routes, ensure_ascii=False, indent=2), flush=True)
    print("[serve-courseloop] " + shlex.join(cmd), flush=True)
    print(f"CL_LLM_TEXT_BASE_URL=http://127.0.0.1:{args.port}/v1", flush=True)
    print("CL_LLM_TEXT_API_KEY=EMPTY", flush=True)
    for key, value in routes["env"].items():
        print(f"{key}={value}", flush=True)
    if args.dry_run:
        return
    if not Path(args.vllm).exists():
        raise FileNotFoundError(args.vllm)
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=args.gpu, OMP_NUM_THREADS="1")
    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    main()
