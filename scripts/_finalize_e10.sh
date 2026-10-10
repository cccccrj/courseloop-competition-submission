#!/usr/bin/env bash
# 等 E10 三门生成完 → 校验清洗 → 干净合并 train/val → 灌题库 → 达标则排队训练。
cd /opt/projects/courseloop
RAG=/opt/miniconda3/envs/rag_env/bin/python
log(){ echo "[$(date +%H:%M:%S)] $*"; }

log "等待 co/os/net 三门生成结束…"
while pgrep -f "build_sft_data --course co" >/dev/null || \
      pgrep -f "build_sft_data --course os" >/dev/null || \
      pgrep -f "build_sft_data --course net" >/dev/null; do
  sleep 60
done
log "三门生成结束。各 intent 计数："
for c in co os net ds-algo; do for i in solve diagnose practice; do
  echo "  $c/$i: $(wc -l < data/sft/$c/$i.jsonl 2>/dev/null||echo 0)"; done; done

log "校验 + 清洗（产出 .clean.jsonl）…"
$RAG -m scripts.validate_sft --clean 2>&1 | tail -30

log "干净合并 → data/sft/train.jsonl / val.jsonl（按 course 分层）…"
$RAG - <<'PY'
import glob, json, os, random
random.seed(42)
SFT="data/sft"
by={}
for p in glob.glob(os.path.join(SFT,"*","*.clean.jsonl")):
    course=os.path.basename(os.path.dirname(p))
    by.setdefault(course,[])
    for line in open(p,encoding="utf-8"):
        line=line.strip()
        if line: by[course].append(line)
train=[]; val=[]
for course,rows in by.items():
    random.shuffle(rows)
    nv=max(1,int(len(rows)*0.05))
    val+=rows[:nv]; train+=rows[nv:]
    print(f"  {course}: {len(rows)} → train {len(rows)-nv}/val {nv}")
random.shuffle(train); random.shuffle(val)
open(os.path.join(SFT,"train.jsonl"),"w",encoding="utf-8").write("\n".join(train)+"\n")
open(os.path.join(SFT,"val.jsonl"),"w",encoding="utf-8").write("\n".join(val)+"\n")
print(f"合计 train {len(train)} / val {len(val)}")
PY

log "E3：把 practice 灌入 co/os/net 题库（保留 ds 精选种子）…"
for cc in co os net; do $RAG -m scripts.practice_to_bank --course $cc --limit 120 2>&1 | tail -3; done

# 校验达标判定：train 行数 ≥ 8000 且存在
TR=$(wc -l < data/sft/train.jsonl 2>/dev/null||echo 0)
log "train.jsonl = $TR 行"
if [ "$TR" -ge 8000 ]; then
  log "达标，排队上卡训练（GPU 队列 FIFO，会等前面任务）…"
  bash /opt/gpu_queue/submit.sh "courseloop-408-sft" "/opt/projects/courseloop" \
    "/opt/sft_env/bin/python scripts/train_sft.py --epochs 2 --out model/loras/courseloop-408" 1
  log "已排队。查看：bash /opt/gpu_queue/status.sh"
else
  log "train 行数不足 8000，暂不自动排队，待人工检查。"
fi
log "@@@ FINALIZE DONE @@@"
