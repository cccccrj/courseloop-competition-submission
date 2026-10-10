"""从旧 held-out val 提取专项任务子集，供任务路由回归门禁使用。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from scripts.test_lora import _intent  # noqa: E402


def prepare(source: Path, out: Path, intent: str) -> dict:
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = []
    for row in rows:
        try:
            gold = json.loads(row["messages"][-1]["content"])
        except (KeyError, IndexError, json.JSONDecodeError):
            continue
        if _intent(gold) == intent:
            selected.append(row)
    if not selected:
        raise RuntimeError(f"旧验证集中没有 intent={intent}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in selected:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {"source": str(source), "out": str(out), "intent": intent, "n": len(selected)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=HERE / "data/sft/val.jsonl")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--intent", required=True)
    args = parser.parse_args()
    prepare(args.source, args.out, args.intent)
