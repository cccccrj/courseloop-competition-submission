"""用已完成的 bulk 诊断单元准备一个不部署的超参探针数据集。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scripts.generate_sft_pilot import COURSES, _sha  # noqa: E402
from scripts.prepare_incremental_sft import _old_replay, _read, _source_keys, _write  # noqa: E402


OUT = HERE / "data" / "diagnose_probe"


def prepare(*, intent: str = "diagnose", candidate_prefix: str = "diagnose",
            out_dir: Path = OUT) -> dict:
    selected = []
    per_course = {}
    for course in COURSES:
        path = (HERE / "data" / "synthetic_bulk_3000" / "candidates"
                / f"{candidate_prefix}_{course}.jsonl")
        rows = _read(path) if path.exists() else []
        accepted = [row for row in rows if row.get("accepted") and row.get("training_sample")]
        accepted.sort(key=lambda row: _sha(["diagnose-probe", row["candidate_id"]]))
        if len(accepted) < 225:
            raise RuntimeError(f"{intent}/{course} 合格 {len(accepted)} < 225")
        chosen = accepted[:225]
        selected.extend({**row["training_sample"], "candidate_id": row["candidate_id"]}
                        for row in chosen)
        per_course[course] = len(chosen)

    targeted = _read(HERE / "data" / "synthetic_targeted_200" / "accepted.jsonl")
    targeted_eval = [row for row in targeted if row.get("intent") == intent]
    target_sources = set().union(*(_source_keys(row) for row in targeted_eval))
    train_sources = set().union(*(_source_keys(row) for row in selected))
    if target_sources & train_sources:
        raise RuntimeError(f"{intent} probe 与 targeted 题源重合")

    old = _read(HERE / "data" / "sft" / "train.jsonl")
    replay = _old_replay(old, 300)
    train = selected + replay
    train.sort(key=lambda row: _sha([intent, "probe-train", row.get("candidate_id"), row["messages"]]))
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "train.jsonl", train)
    _write(out_dir / "targeted_eval.jsonl", targeted_eval)
    # 保留旧文件名，兼容已经排队的诊断探针命令。
    if intent == "diagnose":
        _write(out_dir / "targeted_diagnose.jsonl", targeted_eval)
    report = {
        "intent": intent, "new_samples": len(selected), "old_replay": len(replay), "train": len(train),
        "targeted_eval": len(targeted_eval), "per_course": per_course,
        "targeted_source_overlap": 0, "deployment_candidate": False,
    }
    (out_dir / "prepare_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False, indent=2))
