"""根据最终门禁构建 CourseLoop 任务→LoRA alias 的可部署路由。"""
from __future__ import annotations

import json
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
DEFAULT_ADAPTER = "model/loras/courseloop-408"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _relative(root: Path, value: str) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else root / path)


def _complete(root: Path, value: str) -> bool:
    path = Path(_relative(root, value))
    return (path / "adapter_config.json").exists() and (path / "adapter_model.safetensors").exists()


def build(root: Path = HERE) -> dict:
    selection = _read(root / "data/eval/synth3000_selection.json")
    default = str(selection.get("selected_adapter") or DEFAULT_ADAPTER)
    if not _complete(root, default):
        default = DEFAULT_ADAPTER
    if not _complete(root, default):
        raise RuntimeError(f"默认 LoRA 不完整：{default}")

    desired = {"default": default}
    for task, gate_path in {
        "diagnose": root / "data/diagnose_probe/gate.json",
        "grounded_qa": root / "data/qa_probe/gate.json",
    }.items():
        gate = _read(gate_path)
        candidate = str(gate.get("adapter") or "")
        deployment_passed = gate.get("deployment_passed", gate.get("passed"))
        fallback_candidate = str(gate.get("fallback_adapter") or "")
        fallback_allowed = (
            task == "diagnose"
            and gate.get("fallback_passed") is True
            and default == DEFAULT_ADAPTER
            and fallback_candidate
            and _complete(root, fallback_candidate)
        )
        if deployment_passed and candidate and _complete(root, candidate):
            desired[task] = candidate
        elif fallback_allowed:
            desired[task] = fallback_candidate
        else:
            desired[task] = default
    desired["solve"] = default
    desired["practice"] = default

    adapters: dict[str, str] = {"courseloop-default": _relative(root, default)}
    routes = {"solve": "courseloop-default", "practice": "courseloop-default"}
    for task in ("diagnose", "grounded_qa"):
        path = _relative(root, desired[task])
        if path == adapters["courseloop-default"]:
            routes[task] = "courseloop-default"
        else:
            alias = f"courseloop-{task.replace('_', '-')}"
            adapters[alias] = path
            routes[task] = alias

    result = {
        "created_at": time.strftime("%F %T"),
        "base_model": str(root / "model/Qwen3-14B-4bit"),
        "adapters": adapters,
        "routes": routes,
        "selection_reason": selection.get("reason", "old_sft_until_final_selection"),
        "env": {
            "CL_LLM_SOLVE_MODEL": routes["solve"],
            "CL_LLM_PRACTICE_MODEL": routes["practice"],
            "CL_LLM_DIAGNOSE_MODEL": routes["diagnose"],
            "CL_LLM_GROUNDED_QA_MODEL": routes["grounded_qa"],
        },
    }
    out = root / "data/eval/runtime_routes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
