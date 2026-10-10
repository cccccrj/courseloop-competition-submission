"""CourseLoop 15 小时自动优化编排器。

串联：定向复测 -> 盲审 -> targeted 基线评测排队 -> 3000 放量 -> 放量盲审
-> 增量数据准备 -> 增量 SFT/双评测/自动选优排队。任何门禁失败立即停止，不把
失败数据送入训练。脚本可重复运行，子阶段均可断点续跑。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
RAG_PYTHON = "/opt/projects/conda_data/envs/rag_env/bin/python"
SFT_PYTHON = "/opt/sft_env/bin/python"
QUEUE = "/opt/gpu_queue/submit.sh"
STATUS = HERE / "data" / "optimization_15h_status.json"
RUNTIME_ROUTES = HERE / "data" / "eval" / "runtime_routes.json"
FINAL_REPORT = HERE / "docs" / "17-CourseLoop增量SFT最终选优-20260822.md"
PRIMARY_ADAPTER = HERE / "model" / "loras" / "courseloop-408-synth3000"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _selection_is_final(path: Path) -> bool:
    """Distinguish the pre-rescue primary report from a terminal decision."""
    if not path.exists():
        return False
    try:
        result = _read(path)
    except (OSError, json.JSONDecodeError):
        return False
    if result.get("passed") is True:
        return True
    reason = str(result.get("reason") or "")
    return reason in {
        "primary_and_rescue_failed_keep_old_sft",
        "primary_failed_rescue_error_keep_old_sft",
    } or "rescue" in (result.get("attempts") or {})


def _selection_is_primary_intermediate(path: Path) -> bool:
    """Recognize the exact checkpoint written immediately before rescue."""
    if not path.exists():
        return False
    try:
        result = _read(path)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        result.get("passed") is False
        and result.get("attempt") == "primary"
        and result.get("reason") == "primary_gate_failed"
        and not result.get("attempts")
    )


def _adapter_complete(path: Path) -> bool:
    return (
        (path / "adapter_config.json").exists()
        and (path / "adapter_model.safetensors").exists()
    )


def _write(stage: str, **extra) -> None:
    previous = _read(STATUS) if STATUS.exists() else {}
    if stage != "failed":
        previous.pop("error", None)
    previous.update(stage=stage, updated_at=time.strftime("%F %T"), **extra)
    STATUS.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(previous, ensure_ascii=False, indent=2), flush=True)


def _run(command: list[str]) -> None:
    print("[15h] RUN:", " ".join(command), flush=True)
    subprocess.run(command, cwd=HERE, check=True)


def _wait_for(path: Path, timeout_hours: float = 2.0) -> None:
    deadline = time.time() + timeout_hours * 3600
    while not path.exists():
        if time.time() >= deadline:
            raise TimeoutError(f"等待超时：{path}")
        time.sleep(20)


def _submit(label: str, command: str) -> None:
    _run(["bash", QUEUE, label, str(HERE), command, "1"])


def _finalize_existing_selection(selection: Path) -> dict:
    """Rebuild every deployable artifact before restoring terminal status."""
    final = _read(selection)
    from scripts.build_runtime_routes import build as build_runtime_routes
    from scripts.compare_incremental_sft import _write_final_report
    build_runtime_routes(HERE)
    _write_final_report(final)
    missing = [str(path) for path in (RUNTIME_ROUTES, FINAL_REPORT) if not path.exists()]
    if missing:
        raise RuntimeError(f"最终产物重建失败：{missing}")
    _write(
        "incremental_selection_complete",
        expected_selection=str(selection),
        selected_adapter=final.get("selected_adapter"),
        selection_reason=final.get("reason"),
        runtime_routes=str(RUNTIME_ROUTES),
        final_report=str(FINAL_REPORT),
    )
    return final


def main() -> None:
    selection = HERE / "data" / "eval" / "synth3000_selection.json"
    # A watchdog restart after primary evaluation must not replay API synthesis
    # and blind audits.  Their successful completion is already implied by this
    # exact intermediate report; resume the comparison/rescue chain directly.
    if _selection_is_final(selection):
        _finalize_existing_selection(selection)
        return
    if _selection_is_primary_intermediate(selection):
        _write("submitting_incremental_rescue_resume")
        _submit(
            "courseloop-synth3000-incremental-sft",
            f"{RAG_PYTHON} scripts/compare_incremental_sft.py",
        )
        _write("gpu_jobs_queued", expected_selection=str(selection),
               selected_after_gate="automatic")
        return
    if not selection.exists() and _adapter_complete(PRIMARY_ADAPTER):
        # Training and epoch validation are already durable.  A failure during
        # standalone regression must resume evaluation, never retrain the LoRA.
        _write("submitting_posttrain_evaluation_resume")
        command = (
            f"{SFT_PYTHON} scripts/test_lora.py "
            "--adapter model/loras/courseloop-408-synth3000 "
            "--val data/sft/val.jsonl --tag 408-synth3000-old "
            "--max-new-tokens 768 --refresh-production-prompts"
            f" && {RAG_PYTHON} scripts/compare_incremental_sft.py"
        )
        _submit("courseloop-synth3000-incremental-sft", command)
        _write("gpu_jobs_queued", expected_selection=str(selection),
               selected_after_gate="automatic")
        return

    targeted_report = HERE / "data" / "synthetic_targeted_200" / "report.json"
    _write("waiting_targeted_200")
    _wait_for(targeted_report)
    targeted = _read(targeted_report)
    if not targeted.get("automatic_gate_passed"):
        _write("stopped_targeted_gate_failed", targeted_report=str(targeted_report))
        return

    targeted_audit_path = HERE / "data" / "synthetic_targeted_200" / "blind_audit_report.json"
    targeted_audit = _read(targeted_audit_path) if targeted_audit_path.exists() else None
    if not targeted_audit or not targeted_audit.get("passed"):
        _write("auditing_targeted_50")
        _run([RAG_PYTHON, "scripts/audit_synthetic_samples.py", "--workers", "4"])
        targeted_audit = _read(targeted_audit_path)
    if not targeted_audit.get("passed"):
        _write("stopped_targeted_audit_failed", targeted_audit=str(targeted_audit_path))
        return

    # targeted 数据永不进入训练，可作为诚实外部评测集；先排旧模型基线，让 GPU
    # 与后续数小时 API 放量并行工作。
    if not (HERE / "data" / "eval" / "report_lora_targeted_baseline.json").exists():
        _write("submitting_targeted_baseline")
        _submit(
            "courseloop-targeted-baseline",
            (f"{SFT_PYTHON} scripts/test_lora.py --adapter model/loras/courseloop-408 "
             "--val data/synthetic_targeted_200/accepted.jsonl --tag targeted_baseline "
             "--max-new-tokens 1024"),
        )

    _write("generating_bulk_3000")
    # 双教师调用当前连续零 API 错误；6 并发可把 solve 放量压缩约三分之一，
    # 同时低于先前压测上限，给正式训练/双评测/抢救保留足够时间窗口。
    _run([RAG_PYTHON, "scripts/generate_synthetic_bulk.py", "--workers", "6"])
    bulk_report_path = HERE / "data" / "synthetic_bulk_3000" / "report.json"
    bulk_report = _read(bulk_report_path)
    if not bulk_report.get("passed"):
        _write("stopped_bulk_gate_failed", bulk_report=str(bulk_report_path))
        return

    _write("auditing_bulk_50")
    bulk_dir = HERE / "data" / "synthetic_bulk_3000"
    _run([
        RAG_PYTHON, "scripts/audit_synthetic_samples.py",
        "--input", str(bulk_dir / "accepted.jsonl"),
        "--out", str(bulk_dir / "blind_audit.jsonl"),
        "--report", str(bulk_dir / "blind_audit_report.json"),
        "--n", "50", "--workers", "4",
    ])
    if not _read(bulk_dir / "blind_audit_report.json").get("passed"):
        _write("repairing_bulk_audit_round_1")
        _run([RAG_PYTHON, "scripts/repair_bulk_blind_audit.py"])
        # Re-select exact cell quotas with rejected candidate IDs excluded.
        _run([RAG_PYTHON, "scripts/generate_synthetic_bulk.py", "--workers", "6"])
        repaired_bulk = _read(bulk_dir / "report.json")
        if not repaired_bulk.get("passed"):
            _write("stopped_bulk_repair_gate_failed")
            return
        _write("auditing_bulk_50_round_2")
        _run([
            RAG_PYTHON, "scripts/audit_synthetic_samples.py",
            "--input", str(bulk_dir / "accepted.jsonl"),
            "--out", str(bulk_dir / "blind_audit.jsonl"),
            "--report", str(bulk_dir / "blind_audit_report.json"),
            "--n", "50", "--workers", "4",
        ])
        if not _read(bulk_dir / "blind_audit_report.json").get("passed"):
            _write("stopped_bulk_audit_failed_after_repair")
            return

    _write("preparing_incremental_sft")
    _run([RAG_PYTHON, "scripts/prepare_incremental_sft.py"])

    # FIFO 中 baseline 必定位于训练之前。训练只跑 1 epoch、低学习率并混入旧数据；
    # 训后分别测旧 val 和未训练 targeted 集，比较脚本决定是否采用新 adapter。
    selection = HERE / "data" / "eval" / "synth3000_selection.json"
    if _selection_is_final(selection):
        _finalize_existing_selection(selection)
        return
    else:
        # A failed primary comparison is deliberately written before rescue.
        # On recovery, rerun only the idempotent comparison/rescue chain: the
        # primary adapter and old-val report are already complete, and rescue
        # training resumes from its own checkpoint when present.
        if selection.exists():
            train_command = f"{RAG_PYTHON} scripts/compare_incremental_sft.py"
        else:
            train_command = (
                f"{SFT_PYTHON} scripts/train_sft.py "
                "--adapter model/loras/courseloop-408 "
                "--train data/sft_incremental_3000/train_with_replay.jsonl "
                "--val data/sft_incremental_3000/val_combined.jsonl "
                "--out model/loras/courseloop-408-synth3000 "
                "--checkpoint-dir model/_sft_synth3000_ckpt "
                "--epochs 1 --lr 5e-5 --eval-steps 250 --resume auto --no-test"
                f" && {SFT_PYTHON} scripts/test_lora.py "
                "--adapter model/loras/courseloop-408-synth3000 "
                "--val data/sft/val.jsonl --tag 408-synth3000-old --max-new-tokens 768 "
                "--refresh-production-prompts"
                f" && {RAG_PYTHON} scripts/compare_incremental_sft.py"
            )
        _write("submitting_incremental_sft")
        _submit("courseloop-synth3000-incremental-sft", train_command)
    _write("gpu_jobs_queued", expected_selection=str(selection), selected_after_gate="automatic")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        _write("failed", error=f"{type(exc).__name__}: {str(exc)[:1000]}")
        raise
