"""横评·API 视觉模型组：对固定测试页跑 印刷转写 + 手写答案提取，记录 usage/延迟/成本。

输出：data/ocr/bench/out/<model>/<page_id>.md（印刷转写）
      data/ocr/bench/out/<model>/<page_id>.answers.json（手写答案，仅 choice 页）
      data/ocr/bench/out/<model>/_meta.json（每次调用 tokens/延迟/¥）
用法：
    python -m scripts.ocr_bench.run_api --models gemini-2.5-pro,gemini-2.5-flash,gpt-4o,qwen-vl-max
"""
from __future__ import annotations

import argparse
import json
import os
import time

import httpx
from openai import OpenAI

from scripts.ocr_bench import common
from scripts.ocr_bench.prices import cost_of

TRANSCRIBE = (
    "这是扫描的《计算机组成原理》考卷(双栏、含公式表格电路图、有学生手写作答)。"
    "请忠实转写【印刷体原题】：先左栏后右栏；公式用LaTeX($...$)；表格用Markdown表格；"
    "忽略手写笔迹；电路图/数据通路图无法文字化的用[图:一句话描述]占位；"
    "★看不清的地方写[?]，绝不猜测或补全。直接输出Markdown。"
)
ANSWERS = (
    "这是学生做过的《计算机组成原理》考卷,卷面有手写作答。请只提取【手写答案】：\n"
    "1) 每道单项选择题学生圈选/打勾的选项(①②③④→A/B/C/D);\n"
    "2) 每道填空题空格里的手写内容。\n"
    '只输出JSON:{"选择题":{"1":"A",...},"填空题":{"1":"..."}}。'
    "看不清或没作答标\"[?]\"。★不要编造。"
)


def call(cli, model, prompt, b64, max_tokens):
    t = time.time()
    r = cli.chat.completions.create(
        model=model, max_tokens=max_tokens, temperature=0,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}])
    dt = time.time() - t
    u = r.usage
    pt, ct = (u.prompt_tokens, u.completion_tokens) if u else (0, 0)
    return (r.choices[0].message.content or ""), {
        "latency_s": round(dt, 1), "prompt_tokens": pt, "completion_tokens": ct,
        "cost_rmb": round(cost_of(model, pt, ct), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="gemini-2.5-pro,gemini-2.5-flash,gpt-4o,qwen-vl-max")
    ap.add_argument("--key", default="")
    ap.add_argument("--base-url", default="https://api.zhizengzeng.com/v1")
    args = ap.parse_args()
    key = args.key or __import__("os").environ.get("CL_LLM_API_KEY", "")
    if not key:
        from backend.core.config import settings
        key = settings.llm_api_key
    cli = OpenAI(base_url=args.base_url, api_key=key,
                 http_client=httpx.Client(trust_env=False, timeout=180))
    gt = common.load_gt()

    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        odir = os.path.join(common.OUT, model)
        os.makedirs(odir, exist_ok=True)
        meta = {}
        for _, _, pid in common.TEST_PAGES:
            b64 = common.img_b64(pid)
            # 印刷转写
            try:
                md, m1 = call(cli, model, TRANSCRIBE, b64, 4000)
                open(os.path.join(odir, f"{pid}.md"), "w", encoding="utf-8").write(md)
                meta[f"{pid}.transcribe"] = m1
                print(f"[{model}/{pid}] 转写 {m1['latency_s']}s tok={m1['prompt_tokens']}+{m1['completion_tokens']} ¥{m1['cost_rmb']}", flush=True)
            except Exception as e:  # noqa: BLE001
                meta[f"{pid}.transcribe"] = {"error": str(e)[:120]}
                print(f"[{model}/{pid}] 转写失败 {str(e)[:80]}", flush=True)
            # 手写答案(仅有 choice 真值的页)
            if gt.get(pid, {}).get("choice_answers"):
                try:
                    js, m2 = call(cli, model, ANSWERS, b64, 1500)
                    open(os.path.join(odir, f"{pid}.answers.json"), "w", encoding="utf-8").write(js)
                    meta[f"{pid}.answers"] = m2
                    print(f"[{model}/{pid}] 答案 {m2['latency_s']}s ¥{m2['cost_rmb']}", flush=True)
                except Exception as e:  # noqa: BLE001
                    meta[f"{pid}.answers"] = {"error": str(e)[:120]}
        common.save_json(os.path.join(odir, "_meta.json"), meta)
        tot = sum(v.get("cost_rmb", 0) for v in meta.values())
        print(f"==== {model} 总成本 ¥{round(tot,4)} ====", flush=True)
    print("API 组完成", flush=True)


if __name__ == "__main__":
    main()
