"""批量转写 data/ocr 下全部计组真题 PDF → Markdown（gemini-2.5-flash）。

复用 run_api 的 TRANSCRIBE 提示与 call()。逐页渲染(DPI 170)→转写→落盘，可断点续跑。
产出：
    data/ocr/transcripts/<slug>/page_NN.md   每页转写
    data/ocr/transcripts/<slug>/full.md      全卷合并
    data/ocr/transcripts/_manifest.json      每页 tokens/延迟/¥ + 总计
用法：
    /opt/miniconda3/envs/rag_env/bin/python -m scripts.ocr_bench.transcribe_all
    # 可选：--workers 4 --model gemini-2.5-flash --dpi 170
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from openai import OpenAI

from scripts.ocr_bench import common
from scripts.ocr_bench.run_api import TRANSCRIBE, call

SRC_DIR = os.path.join(common.ROOT, "data", "ocr")
OUT_DIR = os.path.join(common.ROOT, "data", "ocr", "transcripts")
# 可在此登记重复或不应处理的输入文件。
SKIP = {"sample_exam_duplicate.pdf"}


def slug(name: str) -> str:
    base = os.path.splitext(os.path.basename(name))[0]
    return re.sub(r"[^\w一-鿿]+", "_", base).strip("_")


def list_pdfs() -> list[str]:
    pdfs = [
        p for p in glob.glob(os.path.join(SRC_DIR, "*.pdf"))
        if os.path.basename(p) not in SKIP
    ]
    return sorted(pdfs)


def render_page(pdf: str, pno: int, dpi: int, dst: str) -> str:
    import fitz
    d = fitz.open(pdf)
    pix = d[pno - 1].get_pixmap(dpi=dpi)
    pix.save(dst)
    d.close()
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dpi", type=int, default=common.DPI)
    ap.add_argument("--max-tokens", type=int, default=4000)
    args = ap.parse_args()

    from backend.core.config import settings
    cli = OpenAI(base_url="https://api.zhizengzeng.com/v1", api_key=settings.llm_api_key,
                 http_client=httpx.Client(trust_env=False, timeout=180))

    import fitz
    pdfs = list_pdfs()
    # 构造任务列表：(slug, pdf, page_no, out_md, out_png)
    tasks = []
    for pdf in pdfs:
        sl = slug(pdf)
        d = fitz.open(pdf); n = d.page_count; d.close()
        odir = os.path.join(OUT_DIR, sl); os.makedirs(odir, exist_ok=True)
        for pno in range(1, n + 1):
            md_path = os.path.join(odir, f"page_{pno:02d}.md")
            tasks.append((sl, pdf, pno, md_path, os.path.join(odir, f"page_{pno:02d}.png")))
    todo = [t for t in tasks if not (os.path.exists(t[3]) and os.path.getsize(t[3]) > 0)]
    print(f"PDF {len(pdfs)} 份 / 共 {len(tasks)} 页；待转写 {len(todo)} 页（已完成 {len(tasks)-len(todo)} 页跳过）", flush=True)

    # 续跑时合并已有 manifest，避免覆盖成只剩本次 todo
    mpath = os.path.join(OUT_DIR, "_manifest.json")
    meta: dict = json.load(open(mpath, encoding="utf-8")) if os.path.exists(mpath) else {}
    lock = threading.Lock()

    def work(t):
        sl, pdf, pno, md_path, png_path = t
        key = f"{sl}/page_{pno:02d}"
        try:
            if not os.path.exists(png_path):
                render_page(pdf, pno, args.dpi, png_path)
            b64 = base64.b64encode(open(png_path, "rb").read()).decode()
            md, m = call(cli, args.model, TRANSCRIBE, b64, args.max_tokens)
            open(md_path, "w", encoding="utf-8").write(md)
            with lock:
                meta[key] = m
            print(f"[{key}] {m['latency_s']}s tok={m['prompt_tokens']}+{m['completion_tokens']} ¥{m['cost_rmb']}", flush=True)
        except Exception as e:  # noqa: BLE001
            with lock:
                meta[key] = {"error": str(e)[:150]}
            print(f"[{key}] 失败 {str(e)[:100]}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(as_completed([ex.submit(work, t) for t in todo]))

    # 合并每卷 full.md
    for pdf in pdfs:
        sl = slug(pdf)
        odir = os.path.join(OUT_DIR, sl)
        pages = sorted(glob.glob(os.path.join(odir, "page_*.md")))
        parts = [f"# {sl}\n"]
        for p in pages:
            pno = os.path.basename(p).replace("page_", "").replace(".md", "")
            parts.append(f"\n\n---\n\n## 第 {int(pno)} 页\n\n" + open(p, encoding="utf-8").read())
        open(os.path.join(odir, "full.md"), "w", encoding="utf-8").write("".join(parts))

    common.save_json(os.path.join(OUT_DIR, "_manifest.json"), meta)
    ok = [v for v in meta.values() if "cost_rmb" in v]
    err = [k for k, v in meta.items() if "error" in v]
    tot_cost = sum(v.get("cost_rmb", 0) for v in ok)
    tot_in = sum(v.get("prompt_tokens", 0) for v in ok)
    tot_out = sum(v.get("completion_tokens", 0) for v in ok)
    print(f"\n==== 完成 {len(ok)} 页 / 失败 {len(err)} 页 ====", flush=True)
    print(f"总 token: 输入 {tot_in} + 输出 {tot_out} = {tot_in+tot_out}", flush=True)
    print(f"总成本: ¥{round(tot_cost,4)}", flush=True)
    if err:
        print("失败页(重跑本脚本会自动续):", err, flush=True)


if __name__ == "__main__":
    main()
