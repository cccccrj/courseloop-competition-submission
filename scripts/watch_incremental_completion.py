"""Detached watchdog for the final CourseLoop incremental SFT/evaluation chain.

The shared GPU dispatcher records lifecycle but not a child exit code.  This
watchdog therefore treats the final pipeline status plus the selection report
as the success marker.  The report alone is insufficient because the primary
comparison is intentionally written before a possible rescue run.  If the
queued shell disappears before final selection, the idempotent main pipeline
is rerun and submits a resume-safe job.  It never submits while a matching job
is pending/running and caps retries to avoid runaway GPU use.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
STATUS = HERE / "data/optimization_15h_status.json"
SELECTION = HERE / "data/eval/synth3000_selection.json"
WATCH_STATE = HERE / "data/incremental_completion_watch.json"
LOCK = HERE / "data/incremental_completion_watch.lock"
PYTHON = "/opt/projects/conda_data/envs/rag_env/bin/python"
QUEUE = Path("/opt/gpu_queue")
JOB_TOKEN = "courseloop-synth3000-incremental-sft"
RETRY_STAGES = {"gpu_jobs_queued", "incremental_rescue_running", "failed"}
SEMANTIC_STAGES = {
    "semantic_verification_repair_loop", "semantic_reselecting_replacements",
    "semantic_verification_passed_resuming_pipeline",
    "stopped_bulk_audit_failed_after_repair",
}
SEMANTIC_COMMAND = [PYTHON, "-u", "scripts/run_semantic_repair_then_resume.py"]
SEMANTIC_PROCESS_TOKENS = (
    "run_semantic_repair_then_resume.py", "verify_bulk_semantic.py",
    "generate_synthetic_bulk.py", "run_15h_optimization.py",
)


def _selection_is_final(status: dict[str, object]) -> bool:
    """Return true only after compare_incremental_sft finalized all attempts."""
    return SELECTION.exists() and status.get("stage") == "incremental_selection_complete"


def _write(stage: str, **extra: object) -> None:
    payload = {
        "stage": stage,
        "updated_at": time.strftime("%F %T"),
        **extra,
    }
    WATCH_STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


def _job_active() -> bool:
    return any(
        JOB_TOKEN in path.name
        for directory in (QUEUE / "pending", QUEUE / "running")
        if directory.exists()
        for path in directory.glob("*.job")
    )


def _process_active(tokens: tuple[str, ...]) -> bool:
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = path.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if any(token in command for token in tokens):
            return True
    return False


def main(timeout_hours: float = 15.0, max_retries: int = 2) -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return

    deadline = time.time() + timeout_hours * 3600
    retries = 0
    semantic_restarts = 0
    while time.time() < deadline:
        try:
            status = json.loads(STATUS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            status = {}
        if _selection_is_final(status):
            result = json.loads(SELECTION.read_text(encoding="utf-8"))
            _write("complete", retries=retries,
                   semantic_restarts=semantic_restarts,
                   selected_adapter=result.get("selected_adapter"),
                   reason=result.get("reason"))
            return
        stage = str(status.get("stage") or "")
        active = _job_active()
        semantic_active = _process_active(SEMANTIC_PROCESS_TOKENS)
        _write("watching", retries=retries, semantic_restarts=semantic_restarts,
               pipeline_stage=stage, gpu_job_active=active,
               semantic_flow_active=semantic_active)
        if stage in SEMANTIC_STAGES and not semantic_active:
            if semantic_restarts >= max_retries:
                _write("semantic_restart_limit_reached", retries=retries,
                       semantic_restarts=semantic_restarts, pipeline_stage=stage)
                return
            semantic_restarts += 1
            with (HERE / "data/semantic_repair_detached.log").open("a", encoding="utf-8") as log:
                subprocess.Popen(SEMANTIC_COMMAND, cwd=HERE, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            _write("semantic_flow_restarted", retries=retries,
                   semantic_restarts=semantic_restarts, pipeline_stage=stage)
            time.sleep(30)
            continue
        if stage in RETRY_STAGES and not active:
            if retries >= max_retries:
                _write("retry_limit_reached", retries=retries,
                       semantic_restarts=semantic_restarts, pipeline_stage=stage)
                return
            # Avoid racing the status write immediately after queue submission.
            time.sleep(30)
            try:
                refreshed_status = json.loads(STATUS.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                refreshed_status = {}
            if _selection_is_final(refreshed_status) or _job_active():
                continue
            retries += 1
            _write("resubmitting_resume_safe_pipeline", retries=retries, pipeline_stage=stage)
            try:
                subprocess.run([PYTHON, "scripts/run_15h_optimization.py"], cwd=HERE, check=True)
            except subprocess.CalledProcessError as exc:
                _write("resubmit_failed_will_retry", retries=retries,
                       semantic_restarts=semantic_restarts,
                       pipeline_stage=stage, returncode=exc.returncode)
        time.sleep(30)
    _write("timeout", retries=retries, semantic_restarts=semantic_restarts)


if __name__ == "__main__":
    main()
