"""CourseLoop GRPO / RLVR 训练——在 SFT LoRA 基础上攻 solve 短板（格式优先）。

用 sft_env 跑（勿用 rag_env）：
    /opt/sft_env/bin/python scripts/train_grpo.py --max-steps 2      # 冒烟
    /opt/sft_env/bin/python scripts/train_grpo.py --max-steps 300    # 全量

- 续训 SFT 产物 model/loras/courseloop-408（不从冷基座开始）
- 奖励：scripts/grpo_rewards.py（format 主 + check_numeric 正确性 + 复杂度 shape）
- rollout：HF generate（sft_env 无 vLLM），enable_thinking=False（用训练 tokenizer 渲染 prompt 锁死）
- 产物：model/loras/courseloop-408-grpo；训后自动 run_test(tag="grpo") 出 A/B 报告
"""
from __future__ import annotations

import argparse
import os
import sys

# 抗显存碎片：GRPO 生成阶段峰值高，OOM 时常有大量 reserved-but-unallocated。
# 必须在任何 torch/unsloth 导入之前设置；setdefault 以便 launcher 可覆盖。
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SFT_LORA = os.path.join(HERE, "model", "loras", "courseloop-408")
GRPO_DATA = os.path.join(HERE, "data", "grpo", "train.jsonl")
OUT = os.path.join(HERE, "model", "loras", "courseloop-408-grpo")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default=SFT_LORA, help="续训的 SFT LoRA 目录")
    ap.add_argument("--data", default=GRPO_DATA)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--intents", default="solve", help="仅记录用；v1 数据即 solve-only")
    ap.add_argument("--max-seq", type=int, default=2048)
    ap.add_argument("--max-prompt", type=int, default=1024)
    ap.add_argument("--max-completion", type=int, default=640)
    ap.add_argument("--num-gen", type=int, default=6, help="组大小 G")
    ap.add_argument("--batch", type=int, default=6, help="per_device（须为 num-gen 整数倍）")
    ap.add_argument("--accum", type=int, default=8)
    # 生成批 = batch × steps_per_gen。TRL 默认 steps_per_gen=accum → 一次生成 batch×accum
    # 条 completion，单 4090 会 OOM。钉成 1 → 每次只生成 batch 条，显存与 accum 解耦。
    ap.add_argument("--steps-per-gen", type=int, default=1,
                    help="每次生成对应的优化器步数；1=生成批最小(=batch)，防 OOM")
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--beta", type=float, default=0.02)
    ap.add_argument("--max-steps", type=int, default=300)
    ap.add_argument("--no-test", action="store_true")
    args = ap.parse_args()

    import unsloth  # noqa: F401  必须在 trl/transformers 之前
    from unsloth import FastLanguageModel
    from unsloth.chat_templates import get_chat_template
    try:
        from unsloth import PatchFastRL
        PatchFastRL("GRPO", FastLanguageModel)  # 激活 unsloth 的 GRPO patch（若该版本需要）
    except Exception:  # noqa: BLE001
        pass
    from datasets import load_dataset
    from trl import GRPOTrainer, GRPOConfig

    from scripts.grpo_rewards import REWARD_FUNCS, REWARD_WEIGHTS

    # 1) 续训 SFT LoRA（base+adapter 一并加载）
    model, tok = FastLanguageModel.from_pretrained(
        model_name=args.adapter, max_seq_length=args.max_seq,
        load_in_4bit=True, dtype=None, fast_inference=False,  # 无 vLLM
        use_gradient_checkpointing="unsloth",  # 激活重算，长序列下省下大头显存，根治 OOM
    )
    tok = get_chat_template(tok, chat_template="qwen3")
    FastLanguageModel.for_training(model)

    # 2) 数据：prompt_messages → 渲染成字符串（enable_thinking=False，锁死 no-think）
    ds = load_dataset("json", data_files=args.data, split="train")

    def render(ex):
        text = tok.apply_chat_template(
            ex["prompt_messages"], tokenize=False,
            add_generation_prompt=True, enable_thinking=False)
        return {"prompt": text}

    ds = ds.map(render, remove_columns=["prompt_messages"])
    # 校验 no-think 不变量（fail fast）：Qwen3 在 enable_thinking=False 时会预填
    # 一个空的 <think>\n\n</think> 块来跳过推理——它出现才代表 no-think 生效。
    assert "<think>\n\n</think>" in ds[0]["prompt"], \
        "渲染后的 prompt 缺少空 <think></think> 块，no-think 未生效！"
    print(f"[数据] {len(ds)} 条 solve prompt；示例 prompt 尾部：\n...{ds[0]['prompt'][-160:]}\n")

    # 3) GRPOConfig（单 4090、无 vLLM 的 MVP）
    cfg = GRPOConfig(
        output_dir=os.path.join(HERE, "model", "_grpo_ckpt"),
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        steps_per_generation=args.steps_per_gen,  # 钉住生成批，防 OOM（见 --steps-per-gen）
        num_generations=args.num_gen,
        max_prompt_length=args.max_prompt,
        max_completion_length=args.max_completion,
        temperature=1.0, top_p=1.0,           # 组内需多样性，否则优势=0
        learning_rate=args.lr,
        beta=args.beta,                        # KL 到 ref，小而非零，保住 diagnose/practice
        num_iterations=1,
        loss_type="dr_grpo", scale_rewards=False,  # 消除长度偏置，防奖励长 JSON
        epsilon=0.2,
        max_steps=args.max_steps,
        logging_steps=5, save_steps=50, save_total_limit=2,
        optim="adamw_8bit", weight_decay=0.01, warmup_ratio=0.05,
        lr_scheduler_type="linear",
        bf16=True, report_to="none", seed=42,
        use_vllm=False,
        reward_weights=REWARD_WEIGHTS,
    )

    trainer = GRPOTrainer(
        model=model,
        processing_class=tok,                  # trl 0.24: 原 tokenizer=
        reward_funcs=REWARD_FUNCS,
        args=cfg,
        train_dataset=ds,
    )
    trainer.train()

    os.makedirs(args.out, exist_ok=True)
    model.save_pretrained(args.out)
    tok.save_pretrained(args.out)
    print(f"\n✅ GRPO LoRA 已保存到 {args.out}")

    # 4) 训后自动 A/B 评测（失败不影响已存 adapter）
    if not args.no_test:
        try:
            FastLanguageModel.for_inference(model)
            from scripts.test_lora import run_test
            run_test(model, tok, tag="grpo")
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"\n⚠️ 自动测试失败（adapter 已安全保存）：{e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
