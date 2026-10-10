"""CourseLoop QLoRA 训练（unsloth + Qwen3-14B NF4）——一个通用多任务 LoRA。

用 sft_env（隔离环境，勿用 rag_env）跑：
    sft_env python scripts/train_sft.py --epochs 2

- 基座：本地 NF4 model/Qwen3-14B-4bit；也可用 --adapter 从旧 LoRA 增量训练
- 数据：data/sft/train.jsonl（messages 格式，assistant=验证过的 JSON，非思考）
- 产物：model/loras/courseloop-dsalgo/（LoRA 适配器，部署时挂在 FP8 基座上）

训练要点：
- get_chat_template(tokenizer, "qwen3")；只对 assistant 段算 loss（train_on_responses_only）。
- 非思考模式：数据里 assistant 本就无 <think>，模板按对话原样拼接。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Queue jobs redirect stdout to a file; line buffering keeps loss/learning-rate
# telemetry visible to the detached watchdog instead of hiding it until process exit.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass

# 宿主环境历史上可能注入 OMP_NUM_THREADS=0；libgomp 将其视为非法值。
try:
    if int(os.environ.get("OMP_NUM_THREADS", "1")) < 1:
        os.environ["OMP_NUM_THREADS"] = "1"
except ValueError:
    os.environ["OMP_NUM_THREADS"] = "1"

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASE = os.path.join(HERE, "model", "Qwen3-14B-4bit")
TRAIN = os.path.join(HERE, "data", "sft", "train.jsonl")
VAL = os.path.join(HERE, "data", "sft", "val.jsonl")
OUT = os.path.join(HERE, "model", "loras", "courseloop-dsalgo")


def _resolve_resume(value: str | None, checkpoint_dir: str) -> str | None:
    """Resolve auto safely: a first run must not ask Trainer to resume nowhere."""
    if value != "auto":
        return value
    root = Path(checkpoint_dir)
    checkpoints = []
    if root.exists():
        for path in root.glob("checkpoint-*"):
            if not path.is_dir() or not (path / "trainer_state.json").exists():
                continue
            try:
                step = int(path.name.rsplit("-", 1)[-1])
            except ValueError:
                continue
            checkpoints.append((step, path))
    return str(max(checkpoints)[1]) if checkpoints else None


def _periodic_checkpoint_due(global_step: int, interval: int) -> bool:
    """Return whether a recovery-only checkpoint should be emitted now."""
    return interval > 0 and global_step > 0 and global_step % interval == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--adapter", default=None,
                    help="从已有 LoRA 增量训练；提供后加载 base+adapter，并跳过新建 LoRA")
    ap.add_argument("--train", default=TRAIN)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--checkpoint-dir", default=os.path.join(HERE, "model", "_sft_ckpt"),
                    help="检查点目录；增量训练应使用独立目录，避免覆盖旧 SFT 检查点")
    ap.add_argument("--max-seq", type=int, default=4096)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--max-steps", type=int, default=-1,
                    help="覆盖 epoch 的最大优化步数；默认 -1 按 epochs，冒烟测试可设 2")
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--val", default=VAL)
    ap.add_argument("--eval-steps", type=int, default=250)
    ap.add_argument(
        "--checkpoint-steps", type=int, default=250,
        help=("即使按 epoch 评测，也额外按该步数保存可续训 checkpoint；"
              "0 表示关闭。不会改变 epoch 末最优模型选择"),
    )
    ap.add_argument(
        "--eval-strategy",
        choices=("auto", "steps", "epoch"),
        default="auto",
        help=("验证/保存节奏；auto 对完整 epoch 训练按 epoch 保存，"
              "对 max-steps 冒烟或长任务按 steps 保存"),
    )
    ap.add_argument("--no-test", action="store_true", help="训练结束后不自动跑离线测试")
    ap.add_argument("--resume", default=None,
                    help="断点续训：传检查点路径(如 model/_sft_ckpt/checkpoint-750) 或 'auto' 自动找最新；默认 None=从头训")
    args = ap.parse_args()

    from unsloth import FastLanguageModel
    from unsloth.chat_templates import get_chat_template, train_on_responses_only
    from datasets import load_dataset
    from trl import SFTTrainer, SFTConfig
    from transformers import TrainerCallback

    class PeriodicRecoveryCheckpoint(TrainerCallback):
        """Request resumable saves without changing evaluation cadence."""

        def __init__(self, interval: int):
            self.interval = interval

        def on_step_end(self, args, state, control, **kwargs):  # noqa: ANN001, ANN003
            if _periodic_checkpoint_due(state.global_step, self.interval):
                control.should_save = True
            return control

    # 1) 加载 NF4 基座，或加载已有 base+LoRA 做真正的增量训练。
    # FastLanguageModel.from_pretrained 能从 adapter_config.json 自动定位基座。
    source = args.adapter or args.base
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=source,
        max_seq_length=args.max_seq,
        load_in_4bit=True,
        dtype=None,
    )
    tokenizer = get_chat_template(tokenizer, chat_template="qwen3")

    # 2) 冷启动才创建新 LoRA；增量模式直接训练已加载 adapter 的参数。
    if args.adapter:
        FastLanguageModel.for_training(model)
        print(f"[增量 SFT] 起点 adapter：{args.adapter}")
    else:
        model = FastLanguageModel.get_peft_model(
            model,
            r=args.rank,
            lora_alpha=args.alpha,
            lora_dropout=0.0,
            bias="none",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj"],
            use_gradient_checkpointing="unsloth",
            random_state=42,
        )

    # 3) 数据：messages → 文本（非思考，assistant 原样拼接）
    ds = load_dataset("json", data_files=args.train, split="train")

    def fmt(ex):
        text = tokenizer.apply_chat_template(
            ex["messages"], tokenize=False, add_generation_prompt=False,
            enable_thinking=False)
        return {"text": text}

    ds = ds.map(fmt, remove_columns=[c for c in ds.column_names if c != "messages"])

    # 3b) held-out 验证集（用于训中评估 + 自动挑最优检查点）
    val_ds = None
    if args.val and os.path.exists(args.val):
        val_ds = load_dataset("json", data_files=args.val, split="train")
        val_ds = val_ds.map(fmt, remove_columns=[c for c in val_ds.column_names if c != "messages"])

    # 4) Trainer
    cfg = dict(
        dataset_text_field="text",
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        warmup_ratio=0.05,
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        learning_rate=args.lr,
        logging_steps=10,
        optim="adamw_8bit",
        weight_decay=0.01,
        lr_scheduler_type="linear",
        seed=42,
        output_dir=args.checkpoint_dir,
        report_to="none",
        max_length=args.max_seq,  # trl 0.24: 原 max_seq_length 已改名为 max_length
    )
    if val_ds is not None:
        # 完整 epoch 的增量训练必须在 epoch 末保存，否则例如 431 steps、
        # eval_steps=250 时 load_best_model_at_end 会把后 181 步直接丢掉。
        # 显式 max_steps 的冒烟/长任务仍可使用 steps 节奏。
        eval_strategy = args.eval_strategy
        if eval_strategy == "auto":
            eval_strategy = "epoch" if args.max_steps < 0 else "steps"
        eval_cfg = dict(
            eval_strategy=eval_strategy,
            save_strategy=eval_strategy,
            save_total_limit=2,          # 最优 + 最新，封住磁盘增长
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            greater_is_better=False,
            per_device_eval_batch_size=args.batch,
        )
        if eval_strategy == "steps":
            eval_cfg.update(eval_steps=args.eval_steps, save_steps=args.eval_steps)
        cfg.update(eval_cfg)
    else:
        cfg["save_total_limit"] = 1
    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,  # trl 0.24: 原 tokenizer= 已改名为 processing_class=
        train_dataset=ds,
        eval_dataset=val_ds,
        args=SFTConfig(**cfg),
        callbacks=(
            [PeriodicRecoveryCheckpoint(args.checkpoint_steps)]
            if args.checkpoint_steps > 0 else None
        ),
    )

    # 只对 assistant 段算 loss（mask 掉 system/user）
    trainer = train_on_responses_only(
        trainer,
        instruction_part="<|im_start|>user\n",
        response_part="<|im_start|>assistant\n",
    )

    _rfc = _resolve_resume(args.resume, args.checkpoint_dir)
    if args.resume == "auto":
        print(f"[断点恢复] {'续训 ' + _rfc if _rfc else '未发现完整 checkpoint，从头训练'}")
    trainer.train(resume_from_checkpoint=_rfc)

    os.makedirs(args.out, exist_ok=True)
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    print(f"\n✅ LoRA 适配器已保存到 {args.out}（load_best_model_at_end=最优检查点）")

    # 5) 训练结束后自动跑离线测试并出报告（失败不影响已保存的 adapter）
    if not args.no_test:
        try:
            FastLanguageModel.for_inference(model)
            try:
                from test_lora import run_test          # 以 `python scripts/train_sft.py` 运行时
            except ImportError:
                from scripts.test_lora import run_test  # 以 `python -m scripts.train_sft` 运行时
            tag = os.path.basename(os.path.normpath(args.out)).replace("courseloop-", "") or "best"
            run_test(model, tokenizer, tag=tag, val_path=args.val)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"\n⚠️ 自动测试失败（adapter 已安全保存，可后续单独跑 test_lora）：{e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
