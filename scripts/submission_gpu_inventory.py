"""GPU复现盘点，仅输出硬件、白名单版本和模型摘要，避免收集服务凭据。"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def command(args):
    result = subprocess.run(args, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b""):
            sha.update(block)
    return dict(file=path.name, bytes=path.stat().st_size, sha256=sha.hexdigest())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    import torch
    versions = {}
    for package in ("vllm", "torch", "transformers", "tokenizers", "triton", "peft"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    cgroup = {}
    for name in ("cpu.max", "memory.max", "cpuset.cpus.effective"):
        path = Path("/sys/fs/cgroup")/name
        if path.exists():
            cgroup[name] = path.read_text().strip()
    manifests = {}
    for directory in ("Qwen3-14B-FP8", "courseloop-408"):
        folder = args.root / "model" / directory
        entries = [digest(p) for p in sorted(folder.iterdir()) if p.is_file()]
        manifests[directory] = dict(files=entries,
            manifest_sha256=hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest())
    result = dict(created_at=datetime.now(timezone.utc).isoformat(), python=platform.python_version(),
        os=platform.platform(), versions=versions, torch_cuda=torch.version.cuda,
        cuda_available=torch.cuda.is_available(), capability=torch.cuda.get_device_capability(0),
        gpu=command(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,power.limit",
                     "--format=csv,noheader"]),
        cpu=command(["lscpu"]), cgroup=cgroup, model_assets=manifests,
        scope="local FP8 base plus existing CourseLoop rank-16 LoRA inference; no retraining")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print("GPU inventory and model hashes completed", flush=True)


if __name__ == "__main__":
    main()
