"""演示前一条命令自检：数据 → 环境 → 检索质量 → 测试。

三个脚本原本各跑各的，出问题时要挨个回想"刚才是哪一步没过"。串成一条之后：
任何一步不过就非零退出，并且把每一步的耗时和结论摆在一张表里。

用法：
    python -m scripts.verify_all              # 数据体检 + 演示自检 + 检索评测
    python -m scripts.verify_all --with-tests # 再加一遍 pytest（约 3 分钟）
    python -m scripts.verify_all --quick      # 只跑不花钱、不到一秒的那两步

评测那一步只跑检索（`evaluate --only qa`）：诊断评测要真实调用 LLM，
放进"演示前自检"里既慢又花钱，需要时单独跑 `python -m scripts.evaluate --only diagnose`。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

VENV_PY = sys.executable

STEPS = [
    ("数据体检", [VENV_PY, "-m", "scripts.check_data"], "quick"),
    ("演示自检", [VENV_PY, "-m", "scripts.preflight_demo"], "quick"),
    ("检索评测", [VENV_PY, "-m", "scripts.evaluate", "--only", "qa"], "normal"),
    ("单元测试", [VENV_PY, "-m", "pytest", "-q"], "tests"),
]


def run(name: str, cmd: list[str], verbose: bool) -> tuple[bool, float, str]:
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    took = time.time() - t0
    out = (proc.stdout or "") + (proc.stderr or "")
    if verbose or proc.returncode != 0:
        print(out.rstrip())
    tail = [ln for ln in out.strip().splitlines() if ln.strip()]
    return proc.returncode == 0, took, (tail[-1][:100] if tail else "")


def main() -> int:
    ap = argparse.ArgumentParser(description="演示前一条命令自检")
    ap.add_argument("--with-tests", action="store_true", help="额外跑 pytest")
    ap.add_argument("--quick", action="store_true", help="只跑秒级的两步")
    ap.add_argument("--verbose", action="store_true", help="打印每步完整输出")
    args = ap.parse_args()

    results = []
    ok_all = True
    for name, cmd, kind in STEPS:
        if kind == "tests" and not args.with_tests:
            continue
        if args.quick and kind != "quick":
            continue
        print(f"\n▶ {name} …")
        ok, took, tail = run(name, cmd, args.verbose)
        ok_all = ok_all and ok
        results.append((name, ok, took, tail))

    print("\n" + "=" * 62)
    for name, ok, took, tail in results:
        print(f"  {'✓' if ok else '✗'}  {name:<8} {took:6.1f}s  {tail}")
    print("=" * 62)
    print("全部通过。" if ok_all else "有步骤未通过 —— 上面的输出里有原因。")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
