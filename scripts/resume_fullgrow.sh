#!/usr/bin/env bash
# 全量补题的续跑入口。grow_bank 按题干幂等、缺口每轮从库里现算，
# 所以随时中断随时重跑，已入库的题不会重复生成，也不会被清掉。
#
#   bash scripts/resume_fullgrow.sh          # 四门并行续跑
#   bash scripts/resume_fullgrow.sh ds-algo  # 只续跑一门
#
# setsid 让每个任务自成会话，不受启动它的终端或父进程影响。
set -u
cd "$(dirname "$0")/.."
LOGDIR=${LOGDIR:-data/logs/fullgrow}
# 每个知识点最多生成几次。默认 30 而不是 20：门禁否决率实测 32%~47%，
# 一个要补 12 道的知识点按 47% 算得试约 23 次。第一轮给 20 次，co 和 os
# 就出现了"跑完了但没补满"（分别还缺 36 / 67 道）—— 缺口是每轮从库里现算的，
# 补跑一轮即可，但把默认调够能少跑一轮。
ATTEMPTS=${ATTEMPTS:-30}
mkdir -p "$LOGDIR"
COURSES=${@:-"ds-algo net os co"}
for c in $COURSES; do
    if pgrep -f "grow_bank --course $c " > /dev/null; then
        echo "跳过 $c：已经在跑"
        continue
    fi
    # -u 关掉输出缓冲，否则进度行会全堵在缓冲区里，中途看不到否决率。
    setsid nohup ./.venv/bin/python -u -m scripts.grow_bank \
        --course "$c" --kind choice --target 15 \
        --limit-points 100 --max-attempts "$ATTEMPTS" --apply \
        >> "$LOGDIR/$c.log" 2>&1 &
    echo "已启动 $c → $LOGDIR/$c.log"
done
