#!/usr/bin/env bash
# 守候 practice 数据生成完成后，把 QLoRA 训练任务自动提交到共享 GPU 队列。
# 安全前提：只有当 data/sft/train.jsonl 就绪后才 submit，绝不提交空占卡的阻塞任务。
set -u
ROOT=/opt/projects/courseloop
GEN_PID="${1:?need gen pid}"
LOG=/tmp/cl-auto-submit.log
exec >>"$LOG" 2>&1
echo "=== auto-submit watcher start $(date) 盯 PID=$GEN_PID ==="

# 1) 等生成进程退出
while kill -0 "$GEN_PID" 2>/dev/null; do sleep 30; done
echo "生成进程 $GEN_PID 已退出 $(date)"

# 2) 校验 train.jsonl 已合并且非空（practice 满 1500 后脚本才会合并）
for i in $(seq 1 20); do
  if [ -s "$ROOT/data/sft/train.jsonl" ]; then break; fi
  echo "等 train.jsonl 合并... ($i)"; sleep 15
done
if [ ! -s "$ROOT/data/sft/train.jsonl" ]; then
  echo "错误：train.jsonl 未生成，放弃自动提交。请检查 /tmp/sft-gen.log"
  exit 1
fi
TR=$(wc -l < "$ROOT/data/sft/train.jsonl")
VA=$(wc -l < "$ROOT/data/sft/val.jsonl" 2>/dev/null || echo 0)
PR=$(wc -l < "$ROOT/data/sft/practice.jsonl" 2>/dev/null || echo 0)
echo "数据就绪：practice=$PR train=$TR val=$VA"

# 3) 提交到共享队列（1 卡；命令内不设 CUDA_VISIBLE_DEVICES，派发器负责 pin）
bash /opt/gpu_queue/submit.sh \
  "courseloop-sft" \
  "$ROOT" \
  "/opt/sft_env/bin/python scripts/train_sft.py --epochs 2" \
  1
echo "=== 已提交 courseloop-sft 到队列 $(date) ==="
bash /opt/gpu_queue/status.sh
