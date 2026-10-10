"""构造 grounded-QA 低学习率抢救集，不使用 targeted 外部题。"""
from __future__ import annotations

import json
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
SOURCE = HERE / "data" / "qa_probe" / "train.jsonl"
TARGET = HERE / "data" / "qa_probe" / "targeted_eval.jsonl"
OUT = HERE / "data" / "qa_probe_rescue"
SEMANTIC = HERE / "data" / "eval" / "report_targeted_semantic_qa_probe.json"


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def prepare() -> dict:
    rows = _read(SOURCE)
    qa = [row for row in rows if row.get("intent") == "grounded_qa" and not row.get("replay_old_sft")]
    replay = [row for row in rows if row.get("replay_old_sft")]
    if len(qa) < 800 or len(replay) < 200:
        raise RuntimeError(f"QA 抢救数据不足：qa={len(qa)}, replay={len(replay)}")
    target_sources = {str(row.get("source_id")) for row in _read(TARGET) if row.get("source_id")}
    train_sources = {str(row.get("source_id")) for row in qa if row.get("source_id")}
    overlap = target_sources & train_sources
    if overlap:
        raise RuntimeError(f"QA 抢救集与外部题源重叠：{len(overlap)}")
    if SEMANTIC.exists():
        groups = json.loads(SEMANTIC.read_text(encoding="utf-8")).get("by_group", {})
        repeats_by_course = {}
        for course in {str(row.get("course")) for row in qa}:
            accuracy = float(groups.get(f"{course}/grounded_qa", {}).get("task_accuracy", 0.0))
            repeats_by_course[course] = 3 if accuracy < 80.0 else (2 if accuracy < 90.0 else 1)
    else:
        repeats_by_course = {str(row.get("course")): 2 for row in qa}
    train = []
    for row in qa:
        for repeat in range(repeats_by_course[str(row.get("course"))]):
            train.append({**row, "qa_rescue_repeat": repeat + 1})
    train.extend(replay)
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / "train.jsonl", train)
    report = {
        "qa_unique": len(qa), "qa_repeats_by_course": repeats_by_course,
        "old_replay": len(replay),
        "train_total": len(train), "targeted_source_overlap": len(overlap),
        "base_adapter": "model/loras/courseloop-408-qa-probe", "learning_rate": 2e-5,
    }
    (OUT / "prepare_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    prepare()
