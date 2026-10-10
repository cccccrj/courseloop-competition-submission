"""Solve 正确率评测：base(SFT) vs GRPO，用 check_numeric 判答案对错。

数据：data/grpo/train.jsonl 里 code_enabled=False 且 check_numeric 可判的 solve 题
（唯一带 canonical 的来源）。GRPO 仅 50 步实际只训到 ~50 个 prompt，占该池 ~4%，
故此评测对 GRPO ~96% 实为 held-out，in-domain 偏差可忽略。

用法： /opt/sft_env/bin/python scripts/_solve_acc_eval.py --n 120
"""
from __future__ import annotations
import argparse, json, os, sys, time

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

BASE_LORA = os.path.join(HERE, "model", "loras", "courseloop-408")
GRPO_LORA = os.path.join(HERE, "model", "loras", "courseloop-408-grpo")
DATA = os.path.join(HERE, "data", "grpo", "train.jsonl")
OUT = os.path.join(HERE, "data", "eval", "solve_acc_base_vs_grpo.json")


def build_pool(check_numeric):
    rows = [json.loads(l) for l in open(DATA, encoding="utf-8")]
    pool = []
    for r in rows:
        if r.get("intent") != "solve" or r.get("code_enabled") is not False:
            continue
        can = r.get("canonical")
        if can in (None, "null"):
            continue
        try:
            cj = json.loads(can) if isinstance(can, str) else can
        except Exception:
            continue
        gold = str(cj.get("value")) if isinstance(cj, dict) else str(cj)
        try:
            if check_numeric(gold, cj).passed is None:
                continue
        except Exception:
            continue
        pool.append({"msgs": r["prompt_messages"], "canonical": cj,
                     "course": r.get("course", "?")})
    return pool


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120, help="采样题数")
    ap.add_argument("--max-new", type=int, default=768)
    args = ap.parse_args()

    import unsloth  # noqa
    from unsloth import FastLanguageModel
    from unsloth.chat_templates import get_chat_template
    from scripts.test_lora import _extract_json, _generate
    from backend.verification.numeric_checker import check_numeric

    pool = build_pool(check_numeric)
    print(f"[池] 可判 solve 题总数: {len(pool)}", flush=True)
    # 确定性采样（不用 random，等距抽样）
    n = min(args.n, len(pool))
    step = max(1, len(pool) // n)
    sample = pool[::step][:n]
    from collections import Counter
    print(f"[采样] {len(sample)} 题  课程分布: {dict(Counter(s['course'] for s in sample))}", flush=True)

    def eval_model(lora_path, tag):
        print(f"\n=== 加载 {tag}: {lora_path} ===", flush=True)
        model, tok = FastLanguageModel.from_pretrained(
            model_name=lora_path, max_seq_length=2560,
            load_in_4bit=True, dtype=None, fast_inference=False)
        tok = get_chat_template(tok, chat_template="qwen3")
        FastLanguageModel.for_inference(model)
        res = []
        t0 = time.time()
        for i, s in enumerate(sample):
            txt = _generate(model, tok, s["msgs"], args.max_new)
            pred = _extract_json(txt)
            parsed = isinstance(pred, dict)
            ans = str(pred.get("final_answer", "")) if parsed else ""
            try:
                passed = check_numeric(ans, s["canonical"]).passed
            except Exception:
                passed = None
            res.append({"parsed": parsed, "correct": passed is True,
                        "judgable": passed is not None, "course": s["course"]})
            if (i + 1) % 20 == 0:
                acc = 100 * sum(r["correct"] for r in res) / len(res)
                print(f"  {tag} {i+1}/{len(sample)}  当前正确率 {acc:.1f}%  用时 {int(time.time()-t0)}s", flush=True)
        del model
        import torch, gc
        gc.collect(); torch.cuda.empty_cache()
        return res

    base = eval_model(BASE_LORA, "base")
    grpo = eval_model(GRPO_LORA, "grpo")

    def summarize(res):
        n = len(res)
        return {
            "n": n,
            "json_valid_rate": round(100 * sum(r["parsed"] for r in res) / n, 1),
            "solve_acc": round(100 * sum(r["correct"] for r in res) / n, 1),
            "solve_acc_of_judgable": round(
                100 * sum(r["correct"] for r in res) / max(1, sum(r["judgable"] for r in res)), 1),
        }

    sb, sg = summarize(base), summarize(grpo)
    print("\n" + "=" * 60)
    print("Solve 正确率评测  base(SFT) vs GRPO   (check_numeric 判定)")
    print(f"  采样 {len(sample)} 题（grpo可判solve池，GRPO~96%未训过）")
    print("=" * 60)
    print(f"  {'指标':<20}{'base':>8}{'GRPO':>8}{'Δ':>10}")
    for k, label in [("json_valid_rate", "JSON合法率"),
                     ("solve_acc", "解题正确率"),
                     ("solve_acc_of_judgable", "正确率(可判分母)")]:
        d = sg[k] - sb[k]
        arrow = "↑" if d > 0 else ("↓" if d < 0 else "=")
        print(f"  {label:<20}{sb[k]:>8}{sg[k]:>8}{d:>+8.1f} {arrow}")
    print("=" * 60)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"base": sb, "grpo": sg, "n_sample": len(sample)},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n📄 已写入 {OUT}")


if __name__ == "__main__":
    main()
