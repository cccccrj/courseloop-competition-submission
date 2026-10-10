"""Try one compact grounded-QA production contract after final SFT selection.

This task is queued behind the unified-model comparison.  It changes the
shared production prompt only while evaluating the already-trained QA LoRA on
the frozen 50-row set.  The override is retained and routed only if every
deployment gate passes; otherwise it is removed and the default route remains.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Direct execution sets ``sys.path[0]`` to ``scripts/`` rather than the
# repository root.  Add the root before importing production modules so the
# queued ``python scripts/run_qa_contract_rescue.py`` entrypoint behaves the
# same as ``python -m scripts.run_qa_contract_rescue`` and the test imports.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.agents.prompts import (
    GROUNDED_QA_COMPACT_SUFFIX,
    GROUNDED_QA_OVERRIDE,
    GROUNDED_QA_SYSTEM,
)
from scripts.build_runtime_routes import build as build_runtime_routes
from scripts.compare_incremental_sft import (
    HERE,
    OPTIMIZATION_STATUS,
    QA_GATE,
    _ensure_qa_production_report,
)


SELECTION = HERE / "data" / "eval" / "synth3000_selection.json"
OUT = HERE / "data" / "eval" / "qa_contract_rescue.json"
FINAL_REPORT = HERE / "docs" / "18-CourseLoop-QA生产合同救援-20260823.md"
CANDIDATE_OVERRIDE = (
    HERE / "data" / "eval" / "grounded_qa_contract_candidate.txt"
)


def _final_selection_ready() -> bool:
    if not SELECTION.exists() or not OPTIMIZATION_STATUS.exists():
        return False
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    status = json.loads(OPTIMIZATION_STATUS.read_text(encoding="utf-8"))
    terminal = selection.get("passed") is True or bool(selection.get("attempts")) or str(
        selection.get("reason") or ""
    ) in {"primary_failed_rescue_error_keep_old_sft"}
    return terminal and status.get("stage") == "incremental_selection_complete"


def _write_status(result: dict) -> None:
    status = json.loads(OPTIMIZATION_STATUS.read_text(encoding="utf-8"))
    status.update(
        qa_contract_rescue=result.get("result"),
        qa_contract_rescue_report=str(OUT.relative_to(HERE)),
        qa_contract_rescue_document=str(FINAL_REPORT.relative_to(HERE)),
        updated_at=time.strftime("%F %T"),
    )
    OPTIMIZATION_STATUS.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_human_report(result: dict) -> None:
    checks = result.get("deployment_checks") or {}
    lines = [
        "# CourseLoop QA 生产合同救援",
        "",
        f"- 生成时间：{result.get('created_at')}",
        f"- 结果：`{result.get('result')}`",
        f"- 候选合同保留：`{result.get('override_retained')}`",
        f"- QA adapter：`{result.get('adapter')}`",
        f"- 最终 QA 路由：`{result.get('grounded_qa_route')}`",
        f"- 冻结集任务准确率：{result.get('task_accuracy')}%",
        f"- JSON 合法率：{result.get('json_valid_rate')}%",
        f"- 字段完整率：{result.get('keys_complete_rate')}%",
        "",
        "## 部署门禁",
        "",
    ]
    lines.extend(
        f"- [{'x' if passed else ' '}] `{name}`"
        for name, passed in checks.items()
    )
    lines += [
        "",
        "## 证据文件",
        "",
        f"- `{result.get('production_report')}`",
        "- `data/eval/qa_contract_rescue.json`",
        "- `data/eval/runtime_routes.json`",
        "",
        "> 候选合同仅在冻结集全部过门后原子激活；失败或异常时生产继续使用原合同。",
        "",
    ]
    FINAL_REPORT.parent.mkdir(parents=True, exist_ok=True)
    FINAL_REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if not _final_selection_ready():
        raise RuntimeError("统一模型最终选优尚未完成，拒绝提前改变生产 QA 合同")
    gate_before = json.loads(QA_GATE.read_text(encoding="utf-8"))
    if gate_before.get("passed") is not True:
        raise RuntimeError("QA 专项训练期门禁未通过，不允许合同救援")

    compact = GROUNDED_QA_SYSTEM + GROUNDED_QA_COMPACT_SUFFIX
    CANDIDATE_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_OVERRIDE.write_text(compact + "\n", encoding="utf-8")
    # Trial contract is visible only to this process and its evaluator child.
    # Production continues reading the previously activated contract until an
    # atomic replace after every frozen-set gate has passed.
    previous_trial = os.environ.get("CL_GROUNDED_QA_CONTRACT_FILE")
    os.environ["CL_GROUNDED_QA_CONTRACT_FILE"] = str(CANDIDATE_OVERRIDE)
    try:
        report = None
        for attempt in range(2):
            try:
                report = _ensure_qa_production_report()
                break
            except Exception:
                if attempt == 1:
                    CANDIDATE_OVERRIDE.unlink(missing_ok=True)
                    build_runtime_routes(HERE)
                    raise
                # Prediction cache is append-safe, so a transient model-load or
                # evaluator exit resumes completed rows instead of paying twice.
                time.sleep(10)
    finally:
        if previous_trial is None:
            os.environ.pop("CL_GROUNDED_QA_CONTRACT_FILE", None)
        else:
            os.environ["CL_GROUNDED_QA_CONTRACT_FILE"] = previous_trial

    passed = report is not None
    if passed:
        CANDIDATE_OVERRIDE.replace(GROUNDED_QA_OVERRIDE)
    else:
        CANDIDATE_OVERRIDE.unlink(missing_ok=True)
    routes = build_runtime_routes(HERE)
    gate_after = json.loads(QA_GATE.read_text(encoding="utf-8"))
    result = {
        "created_at": time.strftime("%F %T"),
        "result": "passed_compact_contract_deployed" if passed else "failed_reverted",
        "override_retained": GROUNDED_QA_OVERRIDE.exists(),
        "adapter": gate_after.get("adapter"),
        "production_report": gate_after.get("production_report"),
        "task_accuracy": gate_after.get("task_accuracy"),
        "json_valid_rate": gate_after.get("json_valid_rate"),
        "keys_complete_rate": gate_after.get("keys_complete_rate"),
        "deployment_checks": gate_after.get("deployment_checks"),
        "grounded_qa_route": routes.get("routes", {}).get("grounded_qa"),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_human_report(result)
    _write_status(result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
