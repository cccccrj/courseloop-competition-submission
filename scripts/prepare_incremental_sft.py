"""从 3,000 条合格合成数据构造增量 SFT 的训练/验证集。

新数据保留 5%（150 条）作为从未见过的定向验证集；训练部分混入 600 条旧
SFT replay，降低遗忘风险。旧 train/val 文件保持不变。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scripts.generate_sft_pilot import _sha  # noqa: E402
from scripts.test_lora import _intent  # noqa: E402
from backend.agents.prompts import build_diagnose_system  # noqa: E402
from backend.core.courses import get_course  # noqa: E402


DEFAULT_BULK = HERE / "data" / "synthetic_bulk_3000" / "accepted.jsonl"
DEFAULT_BULK_REPORT = HERE / "data" / "synthetic_bulk_3000" / "report.json"
DEFAULT_AUDIT_REPORT = HERE / "data" / "synthetic_bulk_3000" / "blind_audit_report.json"
DEFAULT_SEMANTIC_REPORT = HERE / "data" / "synthetic_bulk_3000" / "semantic_verification_report.json"
DEFAULT_OUT = HERE / "data" / "sft_incremental_3000"
GPU_QUEUE = Path("/opt/gpu_queue")
SPECIALIST_JOB_LABELS = ("courseloop-qa-probe-rescue", "courseloop-diagnose-taxonomy-rescue")


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _wait_for_live_specialists(timeout_seconds: int = 7200) -> None:
    """Avoid reading probe gates while the live automation is still updating them."""
    status_path = HERE / "data/optimization_15h_status.json"
    if not status_path.exists():
        return
    try:
        stage = json.loads(status_path.read_text(encoding="utf-8")).get("stage")
    except (OSError, json.JSONDecodeError):
        return
    if stage not in {"generating_bulk_3000", "auditing_bulk_50", "preparing_incremental_sft"}:
        return
    deadline = time.time() + timeout_seconds
    while True:
        active = []
        for state in ("pending", "running"):
            directory = GPU_QUEUE / state
            if not directory.exists():
                continue
            active.extend(
                path for path in directory.iterdir()
                if any(label in path.name for label in SPECIALIST_JOB_LABELS)
            )
        if not active:
            return
        if time.time() >= deadline:
            raise TimeoutError(
                "等待专项门控超时：" + ", ".join(sorted(path.name for path in active)))
        print("等待专项门控落盘：" + ", ".join(sorted(path.name for path in active)),
              flush=True)
        time.sleep(20)


def _message_hash(row: dict) -> str:
    return _sha(row["messages"])


def _refresh_diagnose_prompt(row: dict, intent: str | None = None) -> dict:
    """Keep diagnosis SFT inputs aligned with the current production contract."""
    resolved = intent or row.get("intent")
    if resolved != "diagnose" or not row.get("messages"):
        return row
    item = dict(row)
    messages = [dict(message) for message in row["messages"]]
    system = {
        "role": "system",
        "content": build_diagnose_system(get_course(str(row.get("course") or "ds-algo"))),
    }
    if messages[0].get("role") == "system":
        messages[0] = system
    else:
        messages.insert(0, system)
    item["messages"] = messages
    item["diagnose_prompt_refreshed"] = True
    return item


def _source_keys(row: dict) -> set[tuple[str, str]]:
    """统一可用于识别底层题源的键；消息不同也不能绕过外部集隔离。"""
    keys = set()
    if row.get("source_candidate_id"):
        keys.add(("source_candidate_id", str(row["source_candidate_id"])))
    if row.get("source_id"):
        keys.add(("source_id", str(row["source_id"])))
    # targeted/bulk 的 DS 变异 candidate_id 直接同源，额外纳入防线。
    if row.get("candidate_id") and row.get("intent") == "diagnose":
        keys.add(("diagnose_candidate_id", str(row["candidate_id"])))
    return keys


def _source_group(row: dict) -> str:
    """同一底层题源的不同任务/变体必须整体进入 train 或 val。"""
    if row.get("source_id"):
        return f"source:{row['source_id']}"
    if row.get("source_candidate_id"):
        return f"candidate:{row['source_candidate_id']}"
    if row.get("candidate_id"):
        return f"candidate:{row['candidate_id']}"
    return f"messages:{_message_hash(row)}"


def _stratified_split(rows: list[dict], val_n: int) -> tuple[list[dict], list[dict]]:
    cells = defaultdict(list)
    for row in rows:
        cells[(row.get("intent", "other"), row.get("course", "?"))].append(row)
    # 先按 cell 规模用最大余数法得到目标，再以题源组为不可拆分单位做确定性贪心。
    total = len(rows)
    exact = {key: val_n * len(values) / total for key, values in cells.items()}
    allocation = {key: int(value) for key, value in exact.items()}
    remaining = val_n - sum(allocation.values())
    order = sorted(cells, key=lambda key: (exact[key] - allocation[key], _sha(key)), reverse=True)
    for key in order[:remaining]:
        allocation[key] += 1

    source_groups = defaultdict(list)
    for row in rows:
        source_groups[_source_group(row)].append(row)
    pending = set(source_groups)
    val, val_counts = [], defaultdict(int)
    while len(val) < val_n:
        room = val_n - len(val)
        candidates = [key for key in pending if len(source_groups[key]) <= room]
        if not candidates:
            raise RuntimeError(f"题源分组后无法精确切出 {val_n} 条验证集，尚缺 {room}")

        def rank(key: str) -> tuple[float, float, str]:
            counts = defaultdict(int)
            for row in source_groups[key]:
                counts[(row.get("intent", "other"), row.get("course", "?"))] += 1
            useful = sum(min(count, max(0, allocation[cell] - val_counts[cell]))
                         for cell, count in counts.items())
            overflow = sum(max(0, val_counts[cell] + count - allocation[cell])
                           for cell, count in counts.items())
            return (useful / len(source_groups[key]), -overflow, _sha(["source-val", key]))

        chosen = max(candidates, key=rank)
        pending.remove(chosen)
        group_rows = source_groups[chosen]
        val.extend(group_rows)
        for row in group_rows:
            val_counts[(row.get("intent", "other"), row.get("course", "?"))] += 1
    train = [row for key in pending for row in source_groups[key]]
    train.sort(key=lambda row: _sha(["incremental-train", _message_hash(row)]))
    val.sort(key=lambda row: _sha(["incremental-val-order", _message_hash(row)]))
    return train, val


def _old_replay(rows: list[dict], n: int) -> list[dict]:
    enriched = []
    for row in rows:
        item = dict(row)
        try:
            gold = json.loads(item["messages"][-1]["content"])
            intent = _intent(gold)
        except Exception:  # noqa: BLE001
            intent = "other"
        item = _refresh_diagnose_prompt(item, intent)
        item["_replay_group"] = f"{item.get('course', '?')}/{intent}"
        enriched.append(item)
    # 每组先拿一条，再按稳定哈希补齐，让小组不被大组完全淹没。
    enriched.sort(key=lambda row: _sha(["replay", row["_replay_group"], _message_hash(row)]))
    chosen, seen_groups = [], set()
    for row in enriched:
        if row["_replay_group"] not in seen_groups:
            chosen.append(row)
            seen_groups.add(row["_replay_group"])
    chosen_hashes = {_message_hash(row) for row in chosen}
    chosen.extend(row for row in enriched if _message_hash(row) not in chosen_hashes)
    out = []
    for row in chosen[:n]:
        row = {key: value for key, value in row.items() if key != "_replay_group"}
        row["replay_old_sft"] = True
        out.append(row)
    return out


def _diagnose_taxonomy_replay(rows: list[dict], n: int,
                              excluded_hashes: set[str] | None = None) -> list[dict]:
    """Sample old diagnosis rows across courses/tags to prevent taxonomy forgetting.

    Bulk synthesis is intentionally task-targeted and therefore does not reproduce the
    historical error-tag distribution.  A random replay sample can miss the rarer tags;
    this sampler cycles through every ``(course, tag)`` bucket before filling by stable
    hash.  Rows already used by the general replay are excluded so the preservation
    budget adds coverage instead of accidental duplicates.
    """
    excluded_hashes = set(excluded_hashes or ())
    candidates: list[tuple[dict, tuple[str, ...]]] = []
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        try:
            gold = json.loads(row["messages"][-1]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            continue
        if _intent(gold) != "diagnose":
            continue
        tags = tuple(sorted({str(tag).strip().lower()
                             for tag in (gold.get("error_tags") or []) if str(tag).strip()}))
        item = _refresh_diagnose_prompt(dict(row), "diagnose")
        if _message_hash(item) in excluded_hashes:
            continue
        item["taxonomy_replay_old_sft"] = True
        candidates.append((item, tags))
        course = str(item.get("course") or "?")
        for tag in tags or ("__untagged__",):
            buckets[(course, tag)].append(item)

    for key, values in buckets.items():
        values.sort(key=lambda row: _sha(["taxonomy-replay", key, _message_hash(row)]))

    selected: list[dict] = []
    selected_hashes: set[str] = set()
    keys = sorted(buckets, key=lambda key: _sha(["taxonomy-bucket", key]))
    while len(selected) < n:
        progressed = False
        for key in keys:
            while buckets[key] and _message_hash(buckets[key][0]) in selected_hashes:
                buckets[key].pop(0)
            if not buckets[key]:
                continue
            row = buckets[key].pop(0)
            row_hash = _message_hash(row)
            selected.append(row)
            selected_hashes.add(row_hash)
            progressed = True
            if len(selected) >= n:
                break
        if not progressed:
            break

    if len(selected) < n:
        remaining = sorted(
            (row for row, _tags in candidates if _message_hash(row) not in selected_hashes),
            key=lambda row: _sha(["taxonomy-replay-fill", _message_hash(row)]),
        )
        selected.extend(remaining[:n - len(selected)])
    return selected[:n]


def prepare(bulk: Path, out_dir: Path, val_n: int, replay_n: int,
            diagnose_replay_n: int,
            bulk_report: Path, audit_report: Path, semantic_report: Path) -> dict:
    if not bulk_report.exists() or not json.loads(bulk_report.read_text(encoding="utf-8")).get("passed"):
        raise RuntimeError("3,000 条放量门禁尚未通过")
    if not audit_report.exists() or not json.loads(audit_report.read_text(encoding="utf-8")).get("passed"):
        raise RuntimeError("放量数据 50 条盲审尚未通过")
    semantic = (json.loads(semantic_report.read_text(encoding="utf-8"))
                if semantic_report.exists() else {})
    if (not semantic.get("passed")
            or semantic.get("selected_target") != 3000
            or semantic.get("verified_accepted") != 3000
            or semantic.get("rejected_current") != 0):
        raise RuntimeError("放量数据 3,000 条全量语义验收尚未通过")
    rows = [_refresh_diagnose_prompt(row) for row in _read(bulk)]
    if len(rows) != 3000 or len({_message_hash(row) for row in rows}) != 3000:
        raise RuntimeError(f"bulk 必须恰有 3000 条唯一数据，当前 {len(rows)}")
    targeted_path = HERE / "data" / "synthetic_targeted_200" / "accepted.jsonl"
    targeted = _read(targeted_path)
    targeted_sources = set().union(*(_source_keys(row) for row in targeted)) if targeted else set()
    bulk_sources = set().union(*(_source_keys(row) for row in rows)) if rows else set()
    source_overlap = targeted_sources & bulk_sources
    if source_overlap:
        preview = sorted(source_overlap)[:10]
        raise RuntimeError(f"bulk 与 targeted 存在题源泄漏 {len(source_overlap)} 个：{preview}")
    new_train, new_val = _stratified_split(rows, val_n)
    new_source_overlap = ({_source_group(row) for row in new_train}
                          & {_source_group(row) for row in new_val})
    if new_source_overlap:
        raise AssertionError(f"增量 train/val 题源泄漏 {len(new_source_overlap)} 个")
    old_train = _read(HERE / "data" / "sft" / "train.jsonl")
    replay = _old_replay(old_train, replay_n)
    diagnose_replay = _diagnose_taxonomy_replay(
        old_train, diagnose_replay_n, {_message_hash(row) for row in replay})
    probe_paths = {
        "diagnose": HERE / "data" / "diagnose_probe" / "gate.json",
        "grounded_qa": HERE / "data" / "qa_probe" / "gate.json",
    }
    probe_gates = {
        intent: (json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)
        for intent, path in probe_paths.items()
    }
    probe_oversample = []
    for intent, gate in probe_gates.items():
        if gate is not None and not gate.get("passed"):
            course = gate.get("oversample_course")
            repeats = max(1, int(gate.get("oversample_repeats", 1)))
            selected = [
                row for row in new_train
                if row.get("intent") == intent and (not course or row.get("course") == course)
            ]
            for repeat in range(repeats):
                probe_oversample.extend(
                    {**row, "probe_oversample": intent,
                     "probe_oversample_repeat": repeat + 1}
                    for row in selected
                )
    train = new_train + replay + diagnose_replay + probe_oversample
    train.sort(key=lambda row: _sha(["combined-train", _message_hash(row)]))
    old_val = _read(HERE / "data" / "sft" / "val.jsonl")
    val = old_val + new_val
    train_hashes = {_message_hash(row) for row in train}
    val = [row for row in val if _message_hash(row) not in train_hashes]
    val_hashes = {_message_hash(row) for row in val}
    if train_hashes & val_hashes:
        raise AssertionError("增量 train/val 泄漏")

    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "new_train.jsonl", new_train)
    _write(out_dir / "new_val.jsonl", new_val)
    _write(out_dir / "old_replay.jsonl", replay)
    _write(out_dir / "old_diagnose_taxonomy_replay.jsonl", diagnose_replay)
    _write(out_dir / "train_with_replay.jsonl", train)
    _write(out_dir / "val_combined.jsonl", val)
    report = {
        "bulk_total": len(rows), "new_train": len(new_train), "new_val": len(new_val),
        "semantic_verification": semantic,
        "old_replay": len(replay),
        "old_diagnose_taxonomy_replay": len(diagnose_replay),
        "old_diagnose_taxonomy_tags": dict(sorted(Counter(
            tag
            for row in diagnose_replay
            for tag in json.loads(row["messages"][-1]["content"]).get("error_tags", [])
        ).items())),
        "train_with_replay": len(train),
        "probe_gates": probe_gates,
        "probe_oversample": len(probe_oversample),
        "probe_oversample_by_intent": {
            intent: sum(row.get("probe_oversample") == intent for row in probe_oversample)
            for intent in probe_paths
        },
        "old_val": len(old_val), "val_combined": len(val),
        "train_val_overlap": len(train_hashes & val_hashes),
        "new_train_val_source_overlap": len(new_source_overlap),
        "targeted_source_overlap": len(source_overlap),
        "diagnose_prompts_refreshed": sum(
            bool(row.get("diagnose_prompt_refreshed")) for row in rows
        ),
        "base_adapter": "model/loras/courseloop-408",
        "recommended": {
            "epochs": 1.0, "learning_rate": 5e-5,
            "output": "model/loras/courseloop-408-synth3000",
            "checkpoint_dir": "model/_sft_synth3000_ckpt",
        },
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 CourseLoop 增量 SFT 数据")
    parser.add_argument("--bulk", type=Path, default=DEFAULT_BULK)
    parser.add_argument("--bulk-report", type=Path, default=DEFAULT_BULK_REPORT)
    parser.add_argument("--audit-report", type=Path, default=DEFAULT_AUDIT_REPORT)
    parser.add_argument("--semantic-report", type=Path, default=DEFAULT_SEMANTIC_REPORT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--val-n", type=int, default=150)
    parser.add_argument("--replay-n", type=int, default=600)
    parser.add_argument("--diagnose-replay-n", type=int, default=900,
                        help="额外按课程/错因标签分层回放的旧诊断样本数")
    args = parser.parse_args()
    _wait_for_live_specialists()
    print(json.dumps(prepare(args.bulk, args.out_dir, args.val_n, args.replay_n,
                             args.diagnose_replay_n,
                             args.bulk_report, args.audit_report, args.semantic_report),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
