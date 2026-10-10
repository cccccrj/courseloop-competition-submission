"""Detached-safe CourseLoop semantic repair loop, then resume the main pipeline."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
PYTHON = "/opt/projects/conda_data/envs/rag_env/bin/python"
BULK = HERE / "data/synthetic_bulk_3000"
STATUS = HERE / "data/optimization_15h_status.json"
MAX_SEMANTIC_ROUNDS = 10
MAX_BLIND_CYCLES = 10


def run(command: list[str]) -> None:
    print("[detached-repair] RUN:", " ".join(command), flush=True)
    subprocess.run(command, cwd=HERE, check=True)


def write_status(stage: str, **extra) -> None:
    current = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    current.update(stage=stage, updated_at=time.strftime("%F %T"), **extra)
    STATUS.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    for cycle in range(1, MAX_BLIND_CYCLES + 1):
        write_status("semantic_verification_repair_loop", repair_cycle=cycle)
        for verify_round in range(1, MAX_SEMANTIC_ROUNDS + 1):
            run([PYTHON, "scripts/verify_bulk_semantic.py", "--workers", "6"])
            report = json.loads((BULK / "semantic_verification_report.json").read_text(encoding="utf-8"))
            if report.get("passed"):
                break
            write_status("semantic_reselecting_replacements", repair_cycle=cycle,
                         verify_round=verify_round,
                         rejected_current=report.get("rejected_current"))
            run([PYTHON, "scripts/generate_synthetic_bulk.py", "--workers", "6"])
        else:
            raise RuntimeError(
                f"semantic verification did not converge after {MAX_SEMANTIC_ROUNDS} replacement rounds")

        write_status("semantic_verification_passed_resuming_pipeline", repair_cycle=cycle,
                     rejected_current=0, semantic_verified_accepted=3000)
        run([PYTHON, "scripts/run_15h_optimization.py"])
        status = json.loads(STATUS.read_text(encoding="utf-8"))
        stage = str(status.get("stage") or "")
        if stage in {"gpu_jobs_queued", "submitting_incremental_sft",
                     "incremental_rescue_running", "incremental_selection_complete"}:
            return
        if stage == "stopped_bulk_audit_failed_after_repair":
            # Archive/quarantine the newly failed disjoint audit, then loop through
            # exact re-selection and semantic verification again.
            run([PYTHON, "scripts/repair_bulk_blind_audit.py"])
            run([PYTHON, "scripts/generate_synthetic_bulk.py", "--workers", "6"])
            continue
        raise RuntimeError(f"main pipeline stopped unexpectedly at stage={stage}")
    raise RuntimeError(
        f"blind audit did not converge after {MAX_BLIND_CYCLES} repair cycles")


if __name__ == "__main__":
    main()
