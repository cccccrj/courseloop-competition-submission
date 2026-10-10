"""构造 GRPO 训练数据（RLVR，solve-only）——从 SFT solve 样本派生 (prompt, 奖励元数据)。

把每条 SFT solve 样本的 gold assistant 去掉（模型要自己生成），并把奖励需要的元数据
平铺成列：gold_keys（字段完整率用）、canonical（正确性校验用，存 JSON 字符串以规避
datasets/Arrow 对 dict/null/nested 混合类型的 unification 报错）、code_enabled（路由）。

prompt 保留为 messages 列表（system+user）；真正的字符串渲染（enable_thinking=False）
放到 train_grpo.py 里用训练用 tokenizer 做，保证与训练/rollout 完全一致。

用法（任意 python，纯 CPU）：
    /opt/miniconda3/envs/rag_env/bin/python -m scripts.build_grpo_data \
        --courses ds-algo,co,os,net --max-per-course 700 --out data/grpo/train.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from scripts.test_lora import _intent
from backend.core.courses import get_course, is_valid_course


def build_rows(course: str, max_n: int | None) -> list[dict]:
    src = os.path.join(HERE, "data", "sft", course, "solve.jsonl")
    if not os.path.exists(src):
        print(f"[跳过] 无 {src}")
        return []
    code_enabled = get_course(course).code_enabled
    out = []
    for line in open(src, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        ex = json.loads(line)
        msgs = ex.get("messages") or []
        if len(msgs) < 2:
            continue
        try:
            gold = json.loads(msgs[-1]["content"])
        except Exception:  # noqa: BLE001
            continue
        if _intent(gold) != "solve":
            continue
        canonical = gold.get("final_answer_canonical")  # 非代码门才有；None/nested 亦可（reward 端判 None）
        out.append({
            "prompt_messages": msgs[:-1],                 # system + user，去掉 gold assistant
            "intent": "solve",
            "course": course,
            "code_enabled": bool(code_enabled),
            "gold_keys": list(gold.keys()),
            "canonical": json.dumps(canonical, ensure_ascii=False),  # JSON 字符串（含 "null"）
        })
        if max_n and len(out) >= max_n:
            break
    print(f"[{course}] solve → {len(out)} 条（code_enabled={code_enabled}）")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--courses", default="ds-algo,co,os,net")
    ap.add_argument("--max-per-course", type=int, default=700, help="每门 solve 采样上限（均衡+控体量）")
    ap.add_argument("--out", default=os.path.join(HERE, "data", "grpo", "train.jsonl"))
    args = ap.parse_args()

    rows = []
    for c in [x.strip() for x in args.courses.split(",") if x.strip()]:
        if not is_valid_course(c):
            print(f"[跳过] 未知 course={c!r}")
            continue
        rows.extend(build_rows(c, args.max_per_course))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 统计 canonical 可判性（预估 correctness 稠密度）
    supported = {"scalar", "percent", "int", "choice", "bits", "ipv4net", "set"}

    def _kind(s):
        try:
            c = json.loads(s)
        except Exception:  # noqa: BLE001
            return None
        return c.get("kind") if isinstance(c, dict) else None

    resolvable = sum(1 for r in rows if _kind(r["canonical"]) in supported)
    print(f"\n✅ 写出 {len(rows)} 条 → {args.out}")
    print(f"   correctness 可判样本（canonical.kind ∈ 支持集）：{resolvable}/{len(rows)}"
          f"（其余靠 format 主奖励 + shape）")


if __name__ == "__main__":
    main()
