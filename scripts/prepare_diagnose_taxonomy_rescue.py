"""Prepare one evidence-based rescue for the diagnosis label contract.

The first focused rescue showed that the model usually repairs the right code but
confuses ``reasoning`` / ``boundary`` / ``calculation``.  This dataset keeps the
same isolated bulk examples and gold labels, while rebuilding their system prompt
from the current production taxonomy.  Targeted evaluation content is never used.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backend.agents.prompts import build_diagnose_system  # noqa: E402
from backend.core.courses import get_course  # noqa: E402


SOURCE = HERE / "data/diagnose_probe_rescue/train.jsonl"
TARGET = HERE / "data/diagnose_probe/targeted_diagnose.jsonl"
OUT = HERE / "data/diagnose_taxonomy_rescue"
MUTATION_TAG = {
    "返回值逻辑破坏": "reasoning",
    "边界常量偏移": "boundary",
    "比较边界翻转": "boundary",
    "算术运算符替换": "calculation",
}


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _unique(rows: list[dict]) -> list[dict]:
    selected: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("candidate_id") or row.get("source_candidate_id") or len(selected))
        selected.setdefault(key, row)
    return list(selected.values())


def _refresh_prompt(row: dict) -> dict:
    item = dict(row)
    messages = [dict(message) for message in row["messages"]]
    messages[0] = {
        "role": "system",
        "content": build_diagnose_system(get_course(str(row.get("course") or "ds-algo"))),
    }
    item["messages"] = messages
    return item


def prepare() -> dict:
    source = _read(SOURCE)
    ds = _unique([
        row for row in source
        if row.get("intent") == "diagnose" and row.get("course") == "ds-algo"
        and not row.get("replay_old_sft")
    ])
    replay = _unique([row for row in source if row.get("replay_old_sft")])
    if len(ds) < 200 or len(replay) < 200:
        raise RuntimeError(f"标签契约抢救数据不足：ds={len(ds)}, replay={len(replay)}")

    # Assert the mapping from the independent mutation generator, without reading
    # any targeted labels or predictions.
    mutation_counts: dict[str, int] = {}
    for row in ds:
        mutation = str(row.get("verified_mutation") or "")
        expected = MUTATION_TAG.get(mutation)
        gold = json.loads(row["messages"][-1]["content"])
        tags = {str(value) for value in gold.get("error_tags") or []}
        if expected is None or expected not in tags:
            raise RuntimeError(
                f"训练标签与变异契约不一致：{mutation!r} -> {sorted(tags)}")
        mutation_counts[mutation] = mutation_counts.get(mutation, 0) + 1

    target_sources = {
        str(row.get("source_candidate_id")) for row in _read(TARGET)
        if row.get("source_candidate_id")
    }
    train_sources = {
        str(row.get("source_candidate_id")) for row in ds
        if row.get("source_candidate_id")
    }
    overlap = target_sources & train_sources
    if overlap:
        raise RuntimeError(f"标签契约抢救与外部题源重叠：{len(overlap)}")

    refreshed_ds = [_refresh_prompt(row) for row in ds]
    refreshed_replay = [_refresh_prompt(row) for row in replay]
    train: list[dict] = []
    for repeat in range(4):
        train.extend({**row, "taxonomy_rescue_repeat": repeat + 1} for row in refreshed_ds)
    train.extend({**row, "taxonomy_replay": True} for row in refreshed_replay)

    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / "train.jsonl", train)
    report = {
        "ds_unique": len(ds),
        "ds_repeats": 4,
        "old_replay": len(replay),
        "train_total": len(train),
        "mutation_counts": mutation_counts,
        "targeted_source_overlap": len(overlap),
        "targeted_eval_content_used": False,
        "production_prompt_refreshed": True,
        "learning_rate": 1.5e-5,
    }
    (OUT / "prepare_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    prepare()
