"""把 DomainMaster 的 OpenAI 兼容 API 配置安全映射为 CourseLoop 演示配置。"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path


MAPPING = {
    "DM_LLM_BASE_URL": "CL_LLM_BASE_URL",
    "DM_LLM_API_KEY": "CL_LLM_API_KEY",
    "DM_LLM_TEXT_BASE_URL": "CL_LLM_TEXT_BASE_URL",
    "DM_LLM_TEXT_API_KEY": "CL_LLM_TEXT_API_KEY",
    "DM_LLM_EXPERT_MODEL": "CL_LLM_EXPERT_MODEL",
    "DM_LLM_WORKHORSE_MODEL": "CL_LLM_WORKHORSE_MODEL",
    "DM_LLM_REASONER_MODEL": "CL_LLM_REASONER_MODEL",
    "DM_LLM_VISION_MODEL": "CL_LLM_VISION_MODEL",
    "DM_LLM_NO_THINK": "CL_LLM_NO_THINK",
}


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def write_env(source: Path, destination: Path, rag_path: Path) -> Path | None:
    src = read_env(source)
    current = read_env(destination) if destination.exists() else {}
    for old, new in MAPPING.items():
        if src.get(old):
            current[new] = src[old]
    current.update({
        "CL_RAG_MODE": "fts5",
        "CL_RAG_FTS_PATH": str(rag_path.resolve()),
        "CL_EMBED_DEVICE": "cpu",
        "CL_SKIP_WARMUP": "true",
    })
    # 无卡演示机禁止遗留的任务级本地端点抢占路由。
    for task in ("DIAGNOSE", "SOLVE", "GROUNDED_QA", "PRACTICE"):
        current.pop(f"CL_LLM_{task}_BASE_URL", None)
        current.pop(f"CL_LLM_{task}_API_KEY", None)
        current.pop(f"CL_LLM_{task}_MODEL", None)
    backup = None
    if destination.exists():
        backup = destination.with_name(destination.name + ".before-demo-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
        backup.write_bytes(destination.read_bytes())
        os.chmod(backup, 0o600)
    destination.write_text("\n".join(f"{k}={current[k]}" for k in sorted(current)) + "\n", encoding="utf-8")
    os.chmod(destination, 0o600)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description="DomainMaster API → CourseLoop .env")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--rag-path", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    src = read_env(args.source)
    available = sorted(new for old, new in MAPPING.items() if src.get(old))
    print({"available_mappings": available, "rag_path": str(args.rag_path)})
    if not args.apply:
        print("dry-run：未写入。加 --apply 生成仅本机可读的 .env。")
        return
    print({"backup": str(write_env(args.source, args.dest, args.rag_path) or "")})


if __name__ == "__main__":
    main()
