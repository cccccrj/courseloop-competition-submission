"""后台监控：等 GRPO 训后 A/B 评测结束，落盘 report_lora_grpo.json 后，
对比 base(report_lora_best.json) vs GRPO 的所有可比指标并打印。"""
import json, os, time, sys

HERE = "/opt/projects/courseloop"
EVAL = os.path.join(HERE, "data", "eval")
BASE = os.path.join(EVAL, "report_lora_best.json")
GRPO = os.path.join(EVAL, "report_lora_grpo.json")
PID = 171836  # 正在跑评测的进程

def alive(pid):
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False

# 记录起始时 grpo 报告的 mtime，确保等到"新"报告
start_mtime = os.path.getmtime(GRPO) if os.path.exists(GRPO) else 0

# 轮询：进程还活着 或 新报告还没落盘 → 继续等（上限 90 分钟）
deadline = time.time() + 90 * 60
while time.time() < deadline:
    fresh = os.path.exists(GRPO) and os.path.getmtime(GRPO) > start_mtime
    if not alive(PID) and fresh:
        break
    if not alive(PID) and not fresh:
        # 进程没了但没新报告 → 评测可能异常退出
        time.sleep(10)
        fresh = os.path.exists(GRPO) and os.path.getmtime(GRPO) > start_mtime
        break
    time.sleep(60)

print("=" * 70)
if not (os.path.exists(GRPO) and os.path.getmtime(GRPO) > start_mtime):
    print("⚠️ 评测进程已结束，但未见新的 report_lora_grpo.json —— 评测可能失败。")
    print("请查看日志：/opt/gpu_queue/logs/000063__courseloop-grpo-50_.log 末尾")
    sys.exit(0)

b = json.load(open(BASE, encoding="utf-8"))
g = json.load(open(GRPO, encoding="utf-8"))

def fmt(x):
    return f"{x:.1f}" if isinstance(x, (int, float)) else str(x)

def delta(bv, gv):
    if isinstance(bv, (int, float)) and isinstance(gv, (int, float)):
        d = gv - bv
        arrow = "↑" if d > 0 else ("↓" if d < 0 else "=")
        return f"{d:+.1f} {arrow}"
    return ""

print(f"CourseLoop GRPO A/B 对比   base(SFT) vs GRPO")
print(f"  val: {g.get('n_total')} 条 | base 用时 {b.get('seconds')}s / grpo 用时 {g.get('seconds')}s")
print("=" * 70)

def cmp_block(title, bd, gd):
    keys = ["json_valid_rate", "keys_complete_rate", "solve_acc",
            "first_wrong_step_acc", "n"]
    labels = {"json_valid_rate": "JSON合法率", "keys_complete_rate": "字段完整率",
              "solve_acc": "解题正确率", "first_wrong_step_acc": "首错定位率", "n": "样本数"}
    rows = []
    for k in keys:
        if k in bd or k in gd:
            bv, gv = bd.get(k), gd.get(k)
            rows.append((labels.get(k, k), bv, gv))
    if not rows:
        return
    print(f"\n【{title}】")
    print(f"  {'指标':<12}{'base':>10}{'GRPO':>10}{'Δ':>12}")
    for name, bv, gv in rows:
        print(f"  {name:<12}{fmt(bv):>10}{fmt(gv):>10}{delta(bv, gv):>14}")

cmp_block("总体", b.get("overall", {}), g.get("overall", {}))

# by_intent
bi_b, bi_g = b.get("by_intent", {}), g.get("by_intent", {})
for intent in sorted(set(bi_b) | set(bi_g)):
    cmp_block(f"intent={intent}", bi_b.get(intent, {}), bi_g.get(intent, {}))

# by_course
bc_b, bc_g = b.get("by_course", {}), g.get("by_course", {})
for course in sorted(set(bc_b) | set(bc_g)):
    cmp_block(f"course={course}", bc_b.get(course, {}), bc_g.get(course, {}))

print("\n" + "=" * 70)
print("解读要点：若各项 Δ 基本为 0 或负 → GRPO 未带来提升（印证之前判断的信号弱）；")
print("         若 solve/首错定位 明显 ↑ 而格式类持平 → GRPO 起了作用。")
