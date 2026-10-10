"""为未通过首轮门禁的 CourseLoop 增量 LoRA 准备一次定向抢救训练集。

抢救只重采样已经通过门禁的 bulk train 与旧 SFT replay，绝不读取 targeted
外部评测样本的内容。语义评测仅用于决定应加权哪个 intent；旧集退化时则增加
旧数据回放。输出可重复生成，顺序由稳定哈希固定。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
DEFAULT_DATA = HERE / "data" / "sft_incremental_3000"
DEFAULT_SELECTION = HERE / "data" / "eval" / "synth3000_selection.json"
DEFAULT_OUT = HERE / "data" / "sft_incremental_3000_rescue"


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _stable_key(row: dict, repeat: int) -> str:
    import hashlib
    payload = json.dumps([repeat, row.get("candidate_id"), row.get("messages")],
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def prepare(data_dir: Path, selection_path: Path, out_dir: Path) -> dict:
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection.get("passed"):
        raise RuntimeError("首轮增量模型已经通过，无需抢救训练")

    new_train = _read(data_dir / "new_train.jsonl")
    old_replay = _read(data_dir / "old_replay.jsonl")
    taxonomy_path = data_dir / "old_diagnose_taxonomy_replay.jsonl"
    diagnose_taxonomy_replay = _read(taxonomy_path) if taxonomy_path.exists() else []
    original = _read(data_dir / "train_with_replay.jsonl")
    checks = selection.get("checks") or {}
    semantic_groups = selection.get("targeted_semantic_by_group") or {}
    semantic = semantic_groups.get("candidate") or {}
    semantic_baseline = semantic_groups.get("baseline") or {}

    weak_intents = sorted({
        group.rsplit("/", 1)[-1]
        for group, metrics in semantic.items()
        if float(metrics.get("task_accuracy", 0)) < 90.0
    })
    if not checks.get("semantic_absolute_90", False) and not weak_intents:
        weak_intents = ["diagnose", "grounded_qa", "solve"]
    if not checks.get("semantic_no_group_drop_gt_2pp", True):
        weak_intents = sorted(set(weak_intents) | {
            group.rsplit("/", 1)[-1]
            for group, baseline_metrics in semantic_baseline.items()
            if float((semantic.get(group) or {}).get("task_accuracy", 0.0))
            < float(baseline_metrics.get("task_accuracy", 0.0)) - 2.0
        })
    if not checks.get("semantic_gain_or_ceiling", True):
        relative_semantic_weak = {
            group.rsplit("/", 1)[-1]
            for group, metrics in semantic.items()
            if (float(metrics.get("task_accuracy", 0.0)) < 97.0
                and float(metrics.get("task_accuracy", 0.0))
                < float((semantic_baseline.get(group) or {}).get("task_accuracy", 0.0)) + 2.0)
        }
        weak_intents = sorted(set(weak_intents) | relative_semantic_weak)
    if (not checks.get("targeted_json_absolute_95", False)
            or not checks.get("targeted_keys_absolute_95", False)
            or not checks.get("targeted_json_not_lower", True)
            or not checks.get("targeted_keys_gain_or_ceiling", True)):
        # 按实际部署组合只强化结构不稳的 intent。已经由通过门禁的专项
        # LoRA 服务的任务不会因为默认 LoRA 的原始输出被无谓重复训练。
        structural_weak = {
            group.rsplit("/", 1)[-1]
            for group, metrics in semantic.items()
            if (float(metrics.get("json_valid_rate", 0)) < 95.0
                or float(metrics.get("keys_complete_rate", 0)) < 95.0)
        }
        if not checks.get("targeted_json_not_lower", True):
            structural_weak |= {
                group.rsplit("/", 1)[-1]
                for group, metrics in semantic.items()
                if float(metrics.get("json_valid_rate", 0.0))
                < float((semantic_baseline.get(group) or {}).get("json_valid_rate", 0.0))
            }
        if not checks.get("targeted_keys_gain_or_ceiling", True):
            structural_weak |= {
                group.rsplit("/", 1)[-1]
                for group, metrics in semantic.items()
                if (float(metrics.get("keys_complete_rate", 0.0)) < 98.0
                    and float(metrics.get("keys_complete_rate", 0.0))
                    < float((semantic_baseline.get(group) or {}).get("keys_complete_rate", 0.0)) + 2.0)
            }
        weak_intents = sorted(set(weak_intents) | structural_weak)

    extras: list[dict] = []
    for row in new_train:
        if row.get("intent") in weak_intents:
            item = dict(row)
            item["rescue_oversample"] = row.get("intent")
            extras.append(item)

    old_regression = not (
        checks.get("old_json_drop_le_1pp", False)
        and checks.get("old_keys_drop_le_1pp", False)
        and checks.get("old_diagnose_step_drop_le_2pp", False)
        and checks.get("old_diagnose_tag_drop_le_2pp", False)
    )
    if old_regression:
        for row in old_replay + diagnose_taxonomy_replay:
            item = dict(row)
            item["rescue_oversample"] = (
                "old_diagnose_taxonomy_replay"
                if row.get("taxonomy_replay_old_sft") else "old_replay"
            )
            extras.append(item)

    # 即便门禁只差一点，也至少强化所有旧 replay，降低第二轮灾难性遗忘风险。
    if not old_regression:
        extras.extend({**row, "rescue_oversample": "old_replay_guard"} for row in old_replay)
        if "diagnose" in weak_intents:
            extras.extend({**row, "rescue_oversample": "old_diagnose_taxonomy_guard"}
                          for row in diagnose_taxonomy_replay)

    combined = [(row, 0) for row in original] + [(row, 1) for row in extras]
    combined.sort(key=lambda value: _stable_key(value[0], value[1]))
    rescue_train = [row for row, _ in combined]

    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "train.jsonl", rescue_train)
    report = {
        "source_selection": str(selection_path),
        "base_adapter": "model/loras/courseloop-408-synth3000",
        "original_train": len(original),
        "extra_rows": len(extras),
        "rescue_train": len(rescue_train),
        "weak_intents": weak_intents,
        "old_regression": old_regression,
        "diagnose_taxonomy_replay_available": len(diagnose_taxonomy_replay),
        "targeted_eval_content_used": False,
        "recommended": {
            "epochs": 1.0,
            "learning_rate": 2e-5,
            "output": "model/loras/courseloop-408-synth3000-rescue",
        },
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 CourseLoop 第二轮定向抢救 SFT")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    print(json.dumps(prepare(args.data_dir, args.selection, args.out_dir),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
