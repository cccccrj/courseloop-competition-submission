"""构造诊断探针抢救集：只用 bulk DS 诊断样本和旧 SFT replay。"""
from __future__ import annotations

import json
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
SOURCE = HERE / "data" / "diagnose_probe" / "train.jsonl"
TARGET = HERE / "data" / "diagnose_probe" / "targeted_diagnose.jsonl"
OUT = HERE / "data" / "diagnose_probe_rescue"


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def prepare() -> dict:
    rows = _read(SOURCE)
    ds = [row for row in rows if row.get("intent") == "diagnose" and row.get("course") == "ds-algo"
          and not row.get("replay_old_sft")]
    replay = [row for row in rows if row.get("replay_old_sft")]
    if len(ds) < 200 or len(replay) < 200:
        raise RuntimeError(f"诊断抢救数据不足：ds={len(ds)}, replay={len(replay)}")
    target_sources = {str(row.get("source_candidate_id")) for row in _read(TARGET)
                      if row.get("source_candidate_id")}
    train_sources = {str(row.get("source_candidate_id")) for row in ds
                     if row.get("source_candidate_id")}
    overlap = target_sources & train_sources
    if overlap:
        raise RuntimeError(f"诊断抢救集与外部题源重叠：{len(overlap)}")

    # 第二阶段从已训练一轮的 probe adapter 继续，只聚焦目标分布；每条 DS
    # 训练样本看 4 次，旧 replay 看 1 次，低学习率抑制遗忘。
    train = []
    for repeat in range(4):
        train.extend({**row, "diagnose_rescue_repeat": repeat + 1} for row in ds)
    train.extend(replay)
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / "train.jsonl", train)
    report = {
        "ds_unique": len(ds), "ds_repeats": 4, "old_replay": len(replay),
        "train_total": len(train), "targeted_source_overlap": len(overlap),
        "base_adapter": "model/loras/courseloop-408-diagnose-probe",
        "learning_rate": 2e-5,
    }
    (OUT / "prepare_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    prepare()
